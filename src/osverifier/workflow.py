"""Globally bounded review pipeline with audited deduplication and early stop."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .backend import BackendCancelled
from .connection_config import redact_backend_text
from .jobs import JobRunner
from .models import (
    ProposalBatch,
    ReviewRequest,
    SourceSpan,
    review_contracts,
    validate_route_audit,
    validate_spans,
)
from .pipeline import RunConfig, number_lines
from .quota import QuotaExhausted
from .reporting import assemble_report, render_markdown
from .scheduling import DEDUPLICATION, FEEDBACK, VALIDATION, VERIFY, Call, PriorityScheduler
from .storage import (
    FrozenRunMismatch,
    atomic_json,
    atomic_text,
    digest,
    package_fingerprint,
    read_json,
    run_lock,
)
from .trajectory import Trajectory
from .workflow_models import (
    DuplicateAudit,
    DuplicatePlan,
    ScopedReview,
    TextDecomposition,
    WorkflowReport,
)


@dataclass(frozen=True)
class WorkflowConfig(RunConfig):
    early_stop_on_reject: bool = True


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Core-owned artifacts covered by the completion checkpoint. Files written by other
# programs into the run directory (GUI job records and logs, Finder metadata, notes)
# may legitimately change after completion and are neither recorded nor verified.
CHECKPOINT_FILES = frozenset(
    {
        "manifest.json",
        "request.json",
        "question.md",
        "candidate-solution.md",
        "report.json",
        "report.md",
        "diagnostics.json",
        "candidates.json",
        "aliases.json",
        "decomposition.json",
        "proposal-order.json",
        "interruption.json",
    }
)
CHECKPOINT_DIRECTORIES = ("jobs", "nodes")
REQUIRED_CHECKPOINT_FILES = ("manifest.json", "report.json")


def checkpointed(relative: str) -> bool:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        return False
    return relative in CHECKPOINT_FILES or (
        len(path.parts) > 1 and path.parts[0] in CHECKPOINT_DIRECTORIES
    )


def checkpoint_hashes(directory: Path) -> dict[str, str]:
    paths = [directory / name for name in CHECKPOINT_FILES]
    for name in CHECKPOINT_DIRECTORIES:
        paths += (directory / name).rglob("*")
    return {
        path.relative_to(directory).as_posix(): file_sha(path)
        for path in sorted(paths)
        if path.is_file()
    }


def verify_checkpoint(directory: Path, hashes: dict[str, str]) -> None:
    # Checkpoints written before the allowlist also hashed GUI-owned files; those
    # entries are ignored, while every recorded core artifact must still match.
    for name in REQUIRED_CHECKPOINT_FILES:
        if name not in hashes:
            raise FrozenRunMismatch(f"completion checkpoint omits {name}")
    for relative, expected in hashes.items():
        if not checkpointed(relative):
            continue
        path = directory / relative
        if not path.is_file():
            raise FrozenRunMismatch(f"completed artifact missing: {relative}")
        if file_sha(path) != expected:
            raise FrozenRunMismatch(f"completed artifact changed: {relative}")


class DecomposingVerifier:
    def __init__(self, backend, config: WorkflowConfig | None = None):
        self.backend = backend
        self.config = config or WorkflowConfig()

    def verify(self, request: ReviewRequest, run_dir: str | Path) -> WorkflowReport:
        directory = Path(run_dir).expanduser().resolve()
        with run_lock(directory):
            run = _Run(self.backend, self.config, request, directory)
            return run.execute()


class _Run:
    def __init__(self, backend, config, request, directory):
        self.backend, self.config, self.request, self.directory = (
            backend,
            config,
            request,
            directory,
        )
        self.stop = threading.Event()
        self.trace = Trajectory(directory)
        self.provenance = {
            "request": request.model_dump(mode="json"),
            "config": asdict(config),
            "backend": backend.identity,
            "package_sha256": package_fingerprint(),
            "workflow": "global_priority_one_text_decomposition_v2",
        }
        self.nodes, self.candidates, self.canonical, self.aliases = [], {}, {}, {}
        self.origins, self.outcomes, self.errors, self.warnings = {}, {}, [], []
        self.proposals_ready = {}
        self.admission_order = []
        self.admission_cursor = 0
        self.dedup_busy = False
        self.admitted = set()
        self.scheduled_issues = set()
        self.decomposition_status = "pending"
        self.stop_basis = None
        self.inputs = {
            "request": request.model_dump(mode="json"),
            "numbered_question": number_lines(request.question),
            "numbered_solution": number_lines(request.candidate_solution),
        }
        self.runner = JobRunner(
            backend,
            directory,
            config.max_attempts,
            self.provenance["package_sha256"],
            self.trace.record,
        )

    def cancel(self):
        cancel = getattr(self.backend, "cancel", None)
        if cancel:
            cancel()

    def execute(self):
        manifest = self.directory / "manifest.json"
        fingerprint = digest(self.provenance)
        if manifest.exists():
            old = read_json(manifest)
            if old["fingerprint"] != fingerprint or digest(old["provenance"]) != fingerprint:
                raise FrozenRunMismatch("workflow inputs, settings, code, or skills changed")
        else:
            atomic_json(manifest, {"fingerprint": fingerprint, "provenance": self.provenance})
        completed = self.directory / "completion.json"
        if completed.exists():
            checkpoint = read_json(completed)
            if checkpoint["fingerprint"] != fingerprint:
                raise FrozenRunMismatch("completion fingerprint mismatch")
            verify_checkpoint(self.directory, checkpoint["artifact_hashes"])
            report = WorkflowReport.model_validate(read_json(self.directory / "report.json"))
            self.trace.record(
                "completed_run_reused",
                decision=report.decision,
                termination_reason=report.termination_reason,
            )
            self.trace.render()
            return report
        (self.directory / "interruption.json").unlink(missing_ok=True)
        atomic_json(self.directory / "request.json", self.request.model_dump(mode="json"))
        atomic_text(self.directory / "question.md", self.request.question)
        atomic_text(self.directory / "candidate-solution.md", self.request.candidate_solution)
        self.trace.record("run_started", fingerprint=fingerprint, backend=self.backend.identity)
        root = ScopedReview(
            scope_id="root",
            objective=(
                "Inspect the current partial progress and whether its route can continue; do not require resolution of the original question."
                if self.request.mode == "partial"
                else "Inspect the complete solution and whether it resolves the question."
            ),
            source_spans=[
                SourceSpan(
                    document="candidate_solution",
                    start_line=1,
                    end_line=len(self.request.candidate_solution.splitlines()),
                )
            ],
        )
        self.add_node(root)
        self.save()
        try:
            order_path = self.directory / "proposal-order.json"
            if order_path.exists():
                saved = read_json(order_path)
                self.admission_order = saved["order"]
                if saved["sha256"] != digest(self.admission_order) or len(
                    {e["scope_id"] for e in self.admission_order}
                ) != len(self.admission_order):
                    raise FrozenRunMismatch("proposal admission order changed")
            self.scheduler = PriorityScheduler(
                self.runner, self.config.workers, self.stop, self.cancel, self.trace
            )
            self.scheduler.spawn(self.propose_node(self.nodes[0]))
            self.scheduler.spawn(self.decompose(root))
            self.scheduler.run()
            if self.stop.is_set():
                for node in self.nodes:
                    if node["status"] == "running" or node["status"] == "queued":
                        node["status"] = "skipped_rejection" if self.stop_basis else "incomplete"
                        self.trace.record(
                            "node_skipped",
                            scope_id=node["scope"]["scope_id"],
                            reason="audited_rejection" if self.stop_basis else "interrupted",
                        )
            self.refresh_nodes()
            report = self.save(finished=True)
            self.trace.record(
                "run_finished",
                decision=report.decision,
                run_status=report.run_status,
                termination_reason=report.termination_reason,
                coverage_complete=report.coverage_complete,
            )
            self.trace.render()
            if report.run_status == "completed":
                atomic_json(
                    completed,
                    {
                        "fingerprint": fingerprint,
                        "artifact_hashes": checkpoint_hashes(self.directory),
                    },
                )
            return report
        except QuotaExhausted as exc:
            self.stop.set()
            self.errors.append(str(exc))
            self.trace.record("run_paused", reason="quota_exhausted", resume="manual")
            report = self.save(finished=True)
            self.trace.render()
            return report
        except BaseException as exc:
            self.stop.set()
            cancel = getattr(self.backend, "cancel", None)
            if cancel:
                cancel()
            self.errors.append(f"Workflow interrupted: {type(exc).__name__}")
            self.trace.record("run_interrupted", error_type=type(exc).__name__)
            self.save(finished=True)
            self.trace.render()
            raise

    def validate_decomposition(self, value):
        lines = self.request.candidate_solution.splitlines(keepends=True)
        cursor, pieces = 1, []
        for segment in value.segments:
            if segment.start_line != cursor or not cursor <= segment.end_line <= len(lines):
                raise ValueError(
                    "decomposition must be an ordered partition without gaps or overlaps"
                )
            pieces.append("".join(lines[segment.start_line - 1 : segment.end_line]))
            cursor = segment.end_line + 1
        if cursor != len(lines) + 1 or "".join(pieces) != self.request.candidate_solution:
            raise ValueError("decomposition must reconstruct the exact original")

    def add_node(self, scope):
        span = scope.source_spans[0]
        focus = "".join(
            self.request.candidate_solution.splitlines(keepends=True)[
                span.start_line - 1 : span.end_line
            ]
        )
        node = {
            "scope": scope.model_dump(mode="json"),
            "status": "queued",
            "decision": None,
            "search_status": "pending",
            "candidate_ids": [],
            "canonical_ids": [],
            "skipped_issue_ids": [],
        }
        self.nodes.append(node)
        path = self.directory / "nodes" / scope.scope_id
        atomic_json(path / "scope.json", node["scope"])
        atomic_text(path / "focus.md", focus)
        self.trace.record(
            "node_created", scope_id=scope.scope_id, source_spans=node["scope"]["source_spans"]
        )

    def combined_batch(self, issues, status="completed"):
        # Only aggregation metadata is normalized; original scope provenance is retained.
        return ProposalBatch(
            scope_id="aggregate",
            search_status=status,
            search_limitations=[],
            issues=[issue.model_copy(update={"scope_id": "aggregate"}) for issue in issues],
        )

    def save(self, finished=False):
        coverage = self.decomposition_status == "completed" and all(
            node["status"] == "completed" and node["search_status"] == "completed"
            for node in self.nodes
        )
        batch = self.combined_batch(
            list(self.canonical.values()), "completed" if coverage else "incomplete"
        )
        report, diagnostics = assemble_report(
            self.request,
            self.backend.identity["kind"],
            batch,
            self.outcomes,
            self.errors,
            finished=finished,
        )
        early = self.stop_basis is not None
        # Every planned job has a validated cached result, yet unresolved or audit-blocked
        # findings, or a search the proposer could not finish (e.g. an unlocatable cited
        # source), prevent a conclusion. Resume replays these caches and cannot change it.
        settled = (
            not self.errors
            and not self.stop.is_set()
            and self.decomposition_status == "completed"
            and all(node["status"] in ("completed", "incomplete") for node in self.nodes)
            and all(self.outcomes.get(key) is not None for key in self.canonical)
        )
        values = report.model_dump(mode="json")
        values.update(
            schema_version="3.0",
            coverage_complete=coverage,
            termination_reason=(
                "audited_rejection"
                if early
                else "all_nodes_reviewed"
                if report.run_status == "completed"
                else "review_inconclusive"
                if settled
                else "execution_incomplete"
            )
            if finished
            else None,
        )
        if finished and early:
            values["run_status"] = "completed"
        result = WorkflowReport.model_validate(values)
        detail = diagnostics.model_dump(mode="json")
        detail.update(
            schema_version="3.0",
            decomposition=self.decomposition_status,
            nodes=self.nodes,
            aliases=self.aliases,
            issue_origins=self.origins,
            candidate_count=len(self.candidates),
            canonical_issue_count=len(self.canonical),
            stop_basis=self.stop_basis,
            coverage_complete=coverage,
            termination_reason=result.termination_reason,
            dedup_warnings=self.warnings,
        )
        detail["limits"] = [
            line for line in detail["limits"] if "decomposition is deferred" not in line
        ]
        if early:
            detail["limits"].append(
                "Workflow intentionally stopped after audited rejection; queued and cancelled work was not reviewed."
            )
        atomic_json(self.directory / "report.json", result.model_dump(mode="json"))
        atomic_json(self.directory / "diagnostics.json", detail)
        atomic_json(
            self.directory / "candidates.json",
            [issue.model_dump(mode="json") for issue in self.candidates.values()],
        )
        atomic_json(self.directory / "aliases.json", self.aliases)
        body = render_markdown(result)
        body += "\n## Workflow coverage\n\n"
        body += f"Termination: {result.termination_reason or 'running'}. Full planned scope coverage: {coverage}.\n\n"
        if early:
            body += "Stopped intentionally after an audited rejection. Remaining work was skipped or cancelled; this is not a completed review of every node.\n\n"
        elif result.termination_reason == "review_inconclusive":
            body += "Every planned review task finished, but unresolved findings or an unfinished search prevent a conclusion. Resuming replays the same results; a revised solution or a human must settle them.\n\n"
        body += "| Node | Original lines | Status | Decision | Candidates | Canonical issues |\n| --- | --- | --- | --- | --- | --- |\n"
        for node in self.nodes:
            sid = node["scope"]["scope_id"]
            span = node["scope"]["source_spans"][0]
            body += f"| [{sid}](nodes/{sid}/scope.json) | {span['start_line']}–{span['end_line']} | {node['status']} | {node['decision'] or '—'} | {len(node['candidate_ids'])} | {len(node['canonical_ids'])} |\n"
        body += "\n[Full observable trajectory](trajectory.md) · [Machine-readable events](trajectory.jsonl)\n"
        atomic_text(self.directory / "report.md", body)
        return result

    def decompose(self, root):
        self.decomposition_status = "running"
        try:
            decomposition = yield Call(
                VERIFY,
                (
                    "decomposer",
                    "decomposer",
                    {**self.inputs, "scope": root.model_dump(mode="json")},
                    TextDecomposition,
                    self.validate_decomposition,
                ),
            )
            self.decomposition_status = "completed"
            atomic_json(
                self.directory / "decomposition.json", decomposition.model_dump(mode="json")
            )
            if len(decomposition.segments) == 1:
                self.trace.record(
                    "scope_retained", scope_id="root", reason="decomposer_returned_one_segment"
                )
            else:
                for index, segment in enumerate(decomposition.segments, 1):
                    self.add_node(
                        ScopedReview(
                            scope_id=f"node-{index:04d}",
                            kind="segment",
                            parent_scope_id="root",
                            objective="Inspect this original-text segment, retaining access to the complete original inputs.",
                            source_spans=[
                                SourceSpan(
                                    document="candidate_solution",
                                    start_line=segment.start_line,
                                    end_line=segment.end_line,
                                )
                            ],
                            context_before=segment.context_before,
                            context_after=segment.context_after,
                        )
                    )
                    self.scheduler.spawn(self.propose_node(self.nodes[-1]))
        except BackendCancelled:
            self.decomposition_status = "skipped_rejection" if self.stop_basis else "incomplete"
            self.trace.record(
                "decomposition_skipped",
                reason="audited_rejection" if self.stop_basis else "interrupted",
            )
        except (FrozenRunMismatch, QuotaExhausted):
            self.decomposition_status = "incomplete"
            raise
        except Exception as exc:
            self.decomposition_status = "failed"
            error = redact_backend_text(self.backend, str(exc))
            self.errors.append(f"Decomposition failed: {type(exc).__name__}: {error}")
            self.trace.record("decomposition_failed", error_type=type(exc).__name__, error=error)
        self.save()

    def propose_node(self, node):
        scope, sid = node["scope"], node["scope"]["scope_id"]
        node["status"] = "running"
        self.trace.record("node_started", scope_id=sid)
        try:

            def validate_batch(value):
                if value.scope_id != sid:
                    raise ValueError("proposal scope mismatch")
                if (
                    self.config.max_issues is not None
                    and len(value.issues) > self.config.max_issues
                ):
                    raise ValueError("proposal exceeds explicit issue cap")
                for issue in value.issues:
                    validate_spans(issue.source_spans, self.request)

            batch = yield Call(
                VERIFY,
                (
                    f"{sid}-proposer",
                    "proposer",
                    {
                        **self.inputs,
                        "scope": scope,
                        "max_issues": self.config.max_issues,
                        "focus_text": (self.directory / "nodes" / sid / "focus.md")
                        .read_bytes()
                        .decode("utf-8"),
                    },
                    ProposalBatch,
                    validate_batch,
                ),
            )
            node["search_status"] = batch.search_status
            # IDs do not depend on completion order or thread timing.
            incoming = []
            for index, issue in enumerate(batch.issues, 1):
                key = f"{sid}-issue-{index:06d}"
                candidate = issue.model_copy(update={"issue_id": key})
                self.candidates[key] = candidate
                self.origins[key] = {
                    "scope_id": sid,
                    "original_issue_id": issue.issue_id,
                    "proposal_artifact": f"jobs/{sid}-proposer/result.json",
                }
                incoming.append(candidate)
                node["candidate_ids"].append(key)
                self.trace.record("issue_proposed", issue_id=key, **self.origins[key])
            self.proposals_ready[sid] = (node, incoming, digest(batch.model_dump(mode="json")))
            self.start_deduplication()
        except BackendCancelled:
            node["status"] = "skipped_rejection" if self.stop_basis else "incomplete"
            self.trace.record(
                "node_skipped",
                scope_id=sid,
                reason="audited_rejection" if self.stop_basis else "interrupted",
            )
        except (FrozenRunMismatch, QuotaExhausted):
            node["status"] = "incomplete"
            raise
        except Exception as exc:
            node["status"] = "failed"
            node["search_status"] = "failed"
            error = redact_backend_text(self.backend, str(exc))
            self.errors.append(f"Node {sid}: {type(exc).__name__}: {error}")
            self.trace.record(
                "node_failed", scope_id=sid, error_type=type(exc).__name__, error=error
            )
        self.save()

    def start_deduplication(self):
        if self.dedup_busy or self.stop.is_set() or not self.proposals_ready:
            return
        # On resume replay the recorded admission order before accepting fresh
        # batches, so every cached dedup input retains the exact same candidates.
        if self.admission_cursor < len(self.admission_order):
            entry = self.admission_order[self.admission_cursor]
            sid = entry["scope_id"]
            if sid not in self.proposals_ready:
                return
            if self.proposals_ready[sid][2] != entry["proposal_sha256"]:
                raise FrozenRunMismatch("admitted proposal changed")
        else:
            sid = next(iter(self.proposals_ready))
            self.admission_order.append(
                {"scope_id": sid, "proposal_sha256": self.proposals_ready[sid][2]}
            )
            atomic_json(
                self.directory / "proposal-order.json",
                {
                    "order": self.admission_order,
                    "sha256": digest(self.admission_order),
                },
            )
        node, incoming, _ = self.proposals_ready.pop(sid)
        self.dedup_busy = True
        self.scheduler.spawn(self.admit_node(node, incoming))

    def admit_node(self, node, incoming):
        sid = node["scope"]["scope_id"]
        try:
            yield from self.deduplicate(sid, incoming)
            node["canonical_ids"] = list(dict.fromkeys(self.resolve(i.issue_id) for i in incoming))
            self.admitted.add(sid)
            for key in node["canonical_ids"]:
                if key not in self.scheduled_issues:
                    self.scheduled_issues.add(key)
                    self.scheduler.spawn(self.review_one(self.canonical[key]))
            self.admission_cursor += 1
            self.dedup_busy = False
            self.refresh_nodes()
            self.start_deduplication()
        except BackendCancelled:
            node["status"] = "skipped_rejection" if self.stop_basis else "incomplete"
            self.trace.record(
                "node_skipped",
                scope_id=sid,
                reason="audited_rejection" if self.stop_basis else "interrupted",
            )
        except (FrozenRunMismatch, QuotaExhausted):
            node["status"] = "incomplete"
            raise
        self.save()

    def refresh_nodes(self):
        for node in self.nodes:
            sid = node["scope"]["scope_id"]
            if sid not in self.admitted or node["status"] in (
                "completed",
                "incomplete",
                "stopped_rejection",
            ):
                continue
            if not self.stop.is_set() and any(
                key not in self.outcomes for key in node["canonical_ids"]
            ):
                continue
            relevant = [self.canonical[key] for key in node["canonical_ids"]]
            outcomes = {
                key: self.outcomes[key] for key in node["canonical_ids"] if key in self.outcomes
            }
            report, diagnostics = assemble_report(
                self.request,
                self.backend.identity["kind"],
                self.combined_batch(relevant, node["search_status"]),
                outcomes,
                [],
                finished=True,
            )
            node["decision"] = report.decision
            node["status"] = (
                "stopped_rejection"
                if self.stop_basis
                else ("completed" if report.run_status == "completed" else "incomplete")
            )
            atomic_json(
                self.directory / "nodes" / sid / "review.json",
                {
                    "scope_id": sid,
                    "scope_only": True,
                    "report": report.model_dump(mode="json"),
                    "diagnostics": diagnostics.model_dump(mode="json"),
                    "note": "Node decisions concern this review scope. Only the top-level report can authorize a human handoff.",
                },
            )
            self.trace.record(
                "node_finished", scope_id=sid, decision=node["decision"], status=node["status"]
            )

    def resolve(self, key):
        while key in self.aliases:
            key = self.aliases[key]["canonical_issue_id"]
        return key

    def deduplicate(self, sid, incoming):
        if not incoming:
            return
        earlier = list(self.canonical.values())
        all_issues = earlier + incoming
        positions = {issue.issue_id: index for index, issue in enumerate(all_issues)}
        new_ids = {issue.issue_id for issue in incoming}
        accepted = {}
        if len(all_issues) > 1:
            payload = {
                **self.inputs,
                "earlier_candidates": [i.model_dump(mode="json") for i in earlier],
                "incoming_candidates": [i.model_dump(mode="json") for i in incoming],
            }
            fallback = self.directory / "nodes" / sid / "dedup-fallback.json"
            if fallback.exists():
                if read_json(fallback)["fingerprint"] != digest(payload):
                    raise FrozenRunMismatch("dedup fallback inputs changed")
                self.warnings.append(
                    f"{sid}: reusing conservative dedup fallback; all incoming issues retained."
                )
                self.canonical.update((issue.issue_id, issue) for issue in incoming)
                return
            try:

                def validate_plan(value):
                    for match in value.matches:
                        if match.issue_id not in new_ids or match.duplicate_of not in positions:
                            raise ValueError("duplicate references unknown candidate")
                        if positions[match.duplicate_of] >= positions[match.issue_id]:
                            raise ValueError("duplicate link must point backwards")

                plan = yield Call(
                    DEDUPLICATION,
                    (f"{sid}-dedup-plan", "deduplicator", payload, DuplicatePlan, validate_plan),
                )
                if plan.matches:
                    expected = {(m.issue_id, m.duplicate_of) for m in plan.matches}

                    def validate_audit(value):
                        if {(c.issue_id, c.duplicate_of) for c in value.checks} != expected:
                            raise ValueError("duplicate audit must cover every exact link")

                    audit = yield Call(
                        DEDUPLICATION,
                        (
                            f"{sid}-dedup-audit",
                            "duplicate_auditor",
                            {**payload, "proposed_matches": plan.model_dump(mode="json")},
                            DuplicateAudit,
                            validate_audit,
                        ),
                    )
                    accepted = {c.issue_id: c for c in audit.checks if c.confirmed}
                    for check in audit.checks:
                        self.trace.record(
                            "duplicate_link_reviewed",
                            issue_id=check.issue_id,
                            duplicate_of=check.duplicate_of,
                            confirmed=check.confirmed,
                            artifact=f"jobs/{sid}-dedup-audit/result.json",
                        )
            except FrozenRunMismatch:
                raise
            except (BackendCancelled, QuotaExhausted):
                raise
            except Exception as exc:
                atomic_json(fallback, {"fingerprint": digest(payload)})
                self.warnings.append(
                    f"{sid}: duplicate grouping failed; all incoming issues retained ({type(exc).__name__})."
                )
                self.trace.record(
                    "dedup_fallback_keep_all", scope_id=sid, error_type=type(exc).__name__
                )
        for issue in incoming:
            if issue.issue_id in accepted:
                target = self.resolve(accepted[issue.issue_id].duplicate_of)
                self.aliases[issue.issue_id] = {
                    "canonical_issue_id": target,
                    "method": "independently_audited_semantic_duplicate",
                    "audit_artifact": f"jobs/{sid}-dedup-audit/result.json",
                }
            else:
                self.canonical[issue.issue_id] = issue

    def review_one(self, issue):
        key = issue.issue_id
        node = next(n for n in self.nodes if n["scope"]["scope_id"] == issue.scope_id)
        try:

            def validate_review(value):
                if value.issue_id != key:
                    raise ValueError("issue review ID mismatch")
                validate_spans(value.source_spans, self.request)

            review = yield Call(
                VALIDATION,
                (
                    f"{key}-validation",
                    "issue_validator",
                    {
                        **self.inputs,
                        "scope": node["scope"],
                        "proposed_issue": issue.model_dump(mode="json"),
                    },
                    review_contracts(self.request.mode)[0],
                    validate_review,
                ),
            )

            def validate_audit(value):
                validate_route_audit(review, value)
                if value.issue_id != key:
                    raise ValueError("feedback audit issue ID mismatch")
                if {d.feedback_id for d in value.decisions} != {
                    f.feedback_id for f in review.feedback_candidates
                }:
                    raise ValueError("feedback audit must cover every exact candidate")

            audit = yield Call(
                FEEDBACK,
                (
                    f"{key}-feedback",
                    "feedback_auditor",
                    {
                        **self.inputs,
                        "proposed_issue": issue.model_dump(mode="json"),
                        "issue_review": review.model_dump(mode="json"),
                    },
                    review_contracts(self.request.mode)[1],
                    validate_audit,
                ),
            )
            artifact = f"jobs/{key}-feedback/result.json"
            outcome = review, audit, artifact
            self.outcomes[key] = outcome
            check, _ = assemble_report(
                self.request,
                self.backend.identity["kind"],
                self.combined_batch([issue]),
                {key: outcome},
                [],
                finished=True,
            )
            self.trace.record(
                "issue_decided",
                issue_id=key,
                scope_id=issue.scope_id,
                decision=check.decision,
                assessment=review.assessment,
                rejection_supported=review.rejection_supported,
                audit_assessment=audit.issue_review_assessment,
                audit_artifact=artifact,
            )
            if check.decision == "REJECT" and self.config.early_stop_on_reject:
                self.stop_basis = {
                    "scope_id": issue.scope_id,
                    "issue_id": key,
                    "audit_artifact": artifact,
                }
                self.stop.set()
                self.trace.record("early_stop", **self.stop_basis, reason="audited_rejection")
                self.cancel()
        except BackendCancelled:
            node["skipped_issue_ids"].append(key)
            self.trace.record(
                "issue_cancelled",
                issue_id=key,
                reason="audited_rejection" if self.stop_basis else "interrupted",
            )
        except (QuotaExhausted, FrozenRunMismatch):
            raise
        except Exception as exc:
            self.outcomes[key] = None
            error = redact_backend_text(self.backend, str(exc))
            self.errors.append(f"{key}: {type(exc).__name__}: {error}")
            self.trace.record("issue_failed", issue_id=key, error_type=type(exc).__name__)
        self.refresh_nodes()
        self.save()
