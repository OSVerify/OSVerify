from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

from .backend import Backend
from .connection_config import redact_backend_text
from .jobs import JobRunner
from .models import (
    ProposalBatch,
    ProposedIssue,
    ReviewReport,
    ReviewRequest,
    ReviewScope,
    SourceSpan,
    review_contracts,
    validate_route_audit,
    validate_spans,
)
from .quota import QuotaExhausted
from .reporting import assemble_report, render_markdown
from .storage import (
    FrozenRunMismatch,
    atomic_json,
    atomic_text,
    digest,
    package_fingerprint,
    read_json,
    run_lock,
)
from .supplied_references import SNAPSHOT, SuppliedReferenceBackend, SuppliedReferences


@dataclass(frozen=True)
class RunConfig:
    max_issues: int | None = None
    workers: int = 16
    max_attempts: int = 2
    # Extra attempts a job may earn by timing out; see JobRunner.call.
    timeout_retries: int = 1

    def __post_init__(self):
        if min(self.workers, self.max_attempts) < 1:
            raise ValueError("workers and max_attempts must be positive")
        if self.timeout_retries < 0:
            raise ValueError("timeout_retries must not be negative")
        if self.max_issues is not None and self.max_issues < 1:
            raise ValueError("max_issues must be positive when supplied; None means no count cap")


class Verifier:
    def __init__(self, backend: Backend, config: RunConfig | None = None):
        self.backend = backend
        self.config = config or RunConfig()

    def verify(
        self,
        request: ReviewRequest,
        run_dir: str | Path,
        *,
        proposals: ProposalBatch | None = None,
        references: str | Path | None = None,
    ) -> ReviewReport:
        """``references``: optional directory of user-supplied files frozen with this run."""
        directory = Path(run_dir).expanduser().resolve()
        with run_lock(directory):
            return self._verify_locked(request, directory, proposals, references)

    def _verify_locked(self, request, directory, proposals, references=None):
        supplied = SuppliedReferences(references) if references is not None else None
        provenance = {
            "request": request.model_dump(mode="json"),
            "provided_proposals": proposals.model_dump(mode="json") if proposals else None,
            "config": asdict(self.config),
            "backend": self.backend.identity,
            "package_sha256": package_fingerprint(),
        }
        if supplied is not None:
            provenance["supplied_references"] = supplied.provenance
        manifest = directory / "manifest.json"
        fingerprint = digest(provenance)
        if manifest.exists():
            old = read_json(manifest)
            if old["fingerprint"] != fingerprint or digest(old["provenance"]) != fingerprint:
                raise FrozenRunMismatch(
                    "request, config, backend, or code/skills changed; use a new run directory"
                )
        else:
            atomic_json(manifest, {"fingerprint": fingerprint, "provenance": provenance})
        catalogue = supplied.freeze(directory) if supplied is not None else None
        (directory / "interruption.json").unlink(missing_ok=True)
        atomic_json(directory / "request.json", request.model_dump(mode="json"))
        atomic_text(directory / "question.md", request.question)
        atomic_text(directory / "candidate-solution.md", request.candidate_solution)
        scope = ReviewScope(
            scope_id="whole_solution",
            objective=("Inspect current partial progress for concrete errors and evidence against continuing its route; incompleteness alone is not an error." if request.mode == "partial" else "Search the submitted full solution for concrete issues and repair obligations."),
            source_spans=[
                SourceSpan(
                    document="candidate_solution",
                    start_line=1,
                    end_line=len(request.candidate_solution.splitlines()),
                )
            ],
        )
        atomic_json(directory / "scope.json", scope.model_dump(mode="json"))
        inputs = {
            "request": request.model_dump(mode="json"),
            "numbered_question": number_lines(request.question),
            "numbered_solution": number_lines(request.candidate_solution),
        }
        # Every role here (proposer, issue validator, feedback auditor) may read them.
        backend = self.backend
        if supplied is not None:
            inputs["supplied_references"] = catalogue
            backend = SuppliedReferenceBackend(
                self.backend, directory / SNAPSHOT, provenance["supplied_references"]["reader"]
            )
        runner = JobRunner(
            backend, directory, self.config.max_attempts, provenance["package_sha256"],
            timeout_retries=self.config.timeout_retries,
        )
        batch = None
        outcomes = {}
        errors = []

        def save(finished=False):
            report, diagnostics = assemble_report(
                request, self.backend.identity["kind"], batch, outcomes, errors, finished=finished
            )
            atomic_json(directory / "diagnostics.json", diagnostics.model_dump(mode="json"))
            atomic_json(directory / "report.json", report.model_dump(mode="json"))
            atomic_text(directory / "report.md", render_markdown(report))
            return report

        def validate_proposal(value):
            if value.scope_id != scope.scope_id:
                raise ValueError("unexpected proposal scope")
            if self.config.max_issues is not None and len(value.issues) > self.config.max_issues:
                raise ValueError("proposal exceeds configured max_issues; no silent truncation")
            for issue in value.issues:
                validate_spans(issue.source_spans, request)

        save()
        try:
            if proposals is not None:
                batch = ProposalBatch.model_validate(proposals.model_dump())
                validate_proposal(batch)
                atomic_json(directory / "provided-proposals.json", batch.model_dump(mode="json"))
            else:
                batch = runner.call(
                    "proposer",
                    "proposer",
                    {
                        **inputs,
                        "scope": scope.model_dump(mode="json"),
                        "max_issues": self.config.max_issues,
                    },
                    ProposalBatch,
                    validate_proposal,
                )
        except QuotaExhausted as exc:
            errors.append(str(exc))
            return save(finished=True)
        except Exception as exc:
            batch = None
            atomic_json(
                directory / "proposal-failure.json",
                {"type": type(exc).__name__, "error": redact_backend_text(self.backend, str(exc))},
            )
            errors.append(
                "Proposal stage failed; inspect jobs/proposer or the provided proposal contract."
            )
            return save(finished=True)
        except BaseException:
            cancel = getattr(self.backend, "cancel", None)
            if cancel is not None:
                cancel()
            errors.append("Proposal stage interrupted; inspect its attempt artifacts and resume.")
            save(finished=True)
            raise
        save()

        def process(index: int, issue: ProposedIssue):
            # Never turn model-supplied IDs into filesystem paths.
            key = f"issue-{index:04d}"

            def validate_issue(value):
                if value.issue_id != issue.issue_id:
                    raise ValueError("issue validator returned another issue's ID")
                validate_spans(value.source_spans, request)

            review = runner.call(
                f"{key}-validation",
                "issue_validator",
                {
                    **inputs,
                    "scope": scope.model_dump(mode="json"),
                    "proposed_issue": issue.model_dump(mode="json"),
                },
                review_contracts(request.mode)[0],
                validate_issue,
            )

            def validate_audit(value):
                validate_route_audit(review, value)
                if value.issue_id != issue.issue_id:
                    raise ValueError("feedback auditor returned another issue's ID")
                expected = {item.feedback_id for item in review.feedback_candidates}
                observed = {item.feedback_id for item in value.decisions}
                if observed != expected:
                    raise ValueError(
                        "audit must cover every exact feedback candidate once, with no extras"
                    )

            audit = runner.call(
                f"{key}-feedback",
                "feedback_auditor",
                {
                    **inputs,
                    "proposed_issue": issue.model_dump(mode="json"),
                    "issue_review": review.model_dump(mode="json"),
                },
                review_contracts(request.mode)[1],
                validate_audit,
            )
            return review, audit, f"jobs/{key}-feedback/result.json"

        pool = ThreadPoolExecutor(max_workers=self.config.workers)
        quota = None
        try:
            pending = {
                pool.submit(process, i, issue): (i, issue) for i, issue in enumerate(batch.issues)
            }
            for future in as_completed(pending):
                index, issue = pending[future]
                try:
                    outcomes[issue.issue_id] = future.result()
                except QuotaExhausted as exc:
                    # No new backend call starts; other chains may still finish from
                    # exact cache hits before this invocation stops.
                    quota = quota or exc
                    continue
                except Exception:
                    outcomes[issue.issue_id] = None
                    errors.append(
                        f"Issue {index:04d} did not finish validation; inspect its jobs/issue-{index:04d}-* artifacts."
                    )
                save()
            if quota is not None:
                raise quota
        except QuotaExhausted as exc:
            errors.append(str(exc))
            return save(finished=True)
        except BaseException:
            cancel = getattr(self.backend, "cancel", None)
            if cancel is not None:
                cancel()
            errors.append("Run interrupted; completed jobs remain cached for resume.")
            save(finished=True)
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        return save(finished=True)


def number_lines(text: str) -> str:
    return "\n".join(f"{index}: {line}" for index, line in enumerate(text.splitlines(), start=1))
