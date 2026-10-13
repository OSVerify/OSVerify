"""Bounded, separately audited report writing over immutable public feedback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from osverifier.backend import stop_process
from osverifier.connection_config import ConnectionConfig
from osverifier.exporting import escape_tex, render_latex
from osverifier.quota import communicate_with_quota_detection
from osverifier.storage import atomic_json, atomic_text, read_json, run_lock

ROOT = Path(__file__).parent


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Placement(Strict):
    ref: int
    section: Literal["Concern", "Check", "Suggestion", "Clarification"]


class Entry(Strict):
    issue_id: str
    title: str
    issue_type: str
    feedback: list[Placement]


class Draft(Strict):
    overview: str
    entries: list[Entry]


class Fidelity(Strict):
    approved: bool
    reasons: list[str]


def source_snapshot(path):
    source = read_json(path)
    if source.get("review_kind") == "paper" and source.get("classification_status") == "pending":
        raise ValueError("Wait for paper severity classification to finish")
    if source.get("run_status") not in ("completed", "incomplete"):
        raise ValueError("Wait for verification to stop before drafting a report")
    if not isinstance(source.get("feedback"), list):
        raise ValueError("A released report.json with feedback is required")
    # Never copy diagnostics/rationales or use model-written unreleased artifacts.
    payload = {
        key: source.get(key)
        for key in (
            "request_id",
            "mode",
            "review_kind",
            "classification_status",
            "decision",
            "run_status",
            "backend_kind",
            "coverage_complete",
            "termination_reason",
        )
    }
    payload["feedback"] = [
        {
            "ref": i,
            **{
                key: item[key]
                for key in (
                    "issue_id",
                    "feedback_id",
                    "kind",
                    "text",
                    "source_spans",
                    "audit_artifact",
                )
            },
        }
        for i, item in enumerate(source["feedback"])
    ]
    metadata = {item["issue_id"]: item for item in source.get("issues", [])}
    payload["issues"] = [
        {"issue_id": key, "severity": metadata.get(key, {}).get("severity", "unknown"), "rationale": metadata.get(key, {}).get("rationale", "")}
        for key in dict.fromkeys(item["issue_id"] for item in payload["feedback"])
    ]
    if source.get("review_kind") == "paper":
        payload.pop("decision", None)
    return payload


def validate_draft(source, draft):
    expected = [item["issue_id"] for item in source["issues"]]
    if [entry.issue_id for entry in draft.entries] != expected:
        raise ValueError("Report must retain each source issue exactly once in original order")
    refs = []
    allowed = {
        "diagnosis": {"Concern", "Check"},
        "title": {"Concern"},
        "check": {"Check"},
        "repair_request": {"Suggestion"},
        "repair_suggestion": {"Suggestion"},
        "clarification_request": {"Clarification"},
    }
    for entry in draft.entries:
        if not entry.title.strip() or not entry.issue_type.strip():
            raise ValueError("Empty report heading")
        for placement in entry.feedback:
            if not 0 <= placement.ref < len(source["feedback"]):
                raise ValueError("Unknown feedback reference")
            item = source["feedback"][placement.ref]
            if item["issue_id"] != entry.issue_id or placement.section not in allowed.get(
                item["kind"], set()
            ):
                raise ValueError("Feedback assigned to an unsupported issue or section")
            refs.append(placement.ref)
    if sorted(refs) != list(range(len(source["feedback"]))):
        raise ValueError("Every released feedback must appear exactly once")


def render(source, draft):
    validate_draft(source, draft)
    body = "\\section*{Overall opinion}\n"
    body += ("Review status: " + escape_tex(source["run_status"]) + ".\n\n") if source.get("review_kind") == "paper" else (
        "Decision: "
        + escape_tex(source["decision"] or "Pending")
        + ". Run: "
        + escape_tex(source["run_status"])
        + ".\n\n"
    )
    body += (
        "This is an editorial presentation of automated review; it is not a proof certificate.\n\n"
    )
    if source.get("mode") == "partial":
        body += "Partial-progress review: PASS permits continuing, not completion or certified correctness. REVISE does not guarantee repairability. REJECT concerns the current route.\n\n"
    if source.get("backend_kind") == "scripted_demo":
        body += "\\textbf{SCRIPTED DEMO: no live mathematical judgments.}\n\n"
    if source["run_status"] != "completed":
        body += "\\textbf{Review is unfinished; feedback is partial.}\n\n"
    if source.get("coverage_complete") is not None:
        body += "Full planned scope coverage: " + str(source["coverage_complete"]) + ".\n\n"
    if source.get("termination_reason") == "audited_rejection":
        body += "Stopped after an audited rejection; remaining work was skipped or cancelled.\n\n"
    if source.get("review_kind") == "paper":
        body += "Severity classification: " + escape_tex(source.get("classification_status") or "unknown") + ".\n\n"
    body += render_latex(draft.overview, fragment=True)
    for severity, label in (
        ("major", "Major Imprecisions"),
        ("minor", "Minor Imprecisions"),
        ("unknown", "Unclassified Imprecisions and Clarifications"),
    ):
        entries = [
            e
            for e in draft.entries
            if next(i["severity"] for i in source["issues"] if i["issue_id"] == e.issue_id)
            == severity
        ]
        if not entries:
            continue
        body += "\\section{" + label + "}\n"
        if severity == "unknown":
            body += "The source report does not provide a released major/minor classification.\n\n"
        for entry in entries:
            body += "\\subsection{" + escape_tex(entry.title) + "}\n"
            items = [source["feedback"][p.ref] for p in entry.feedback]
            locations = dict.fromkeys(
                f"{s['document']} lines {s['start_line']}–{s['end_line']}"
                for item in items
                for s in item["source_spans"]
            )
            body += (
                "\\paragraph{Location} "
                + escape_tex("; ".join(locations) or "Not supplied.")
                + "\n\n"
            )
            rationale = next(i.get("rationale", "") for i in source["issues"] if i["issue_id"] == entry.issue_id)
            if rationale:
                body += "\\paragraph{Classification basis} " + escape_tex(rationale) + "\n\n"
            body += "\\paragraph{Type} " + escape_tex(entry.issue_type) + "\n\n"
            for section in ("Concern", "Check", "Suggestion", "Clarification"):
                selected = [p for p in entry.feedback if p.section == section]
                if not selected and section == "Clarification":
                    continue
                body += "\\paragraph{" + section + "}\n"
                if not selected:
                    body += (
                        "No separate "
                        + section.lower()
                        + " was supplied in the released feedback.\n\n"
                    )
                for placement in selected:
                    item = source["feedback"][placement.ref]
                    body += render_latex(item["text"], fragment=True)
                    body += (
                        "\n{\\footnotesize Source: "
                        + escape_tex(
                            item["issue_id"]
                            + "/"
                            + item["feedback_id"]
                            + "; "
                            + item["audit_artifact"]
                        )
                        + "}\n\n"
                    )
    if not draft.entries:
        body += "No feedback text was released.\n"
    return render_latex("", title=f"Referee Report on “{source['request_id']}”").replace(
        "\\end{document}", body + "\\end{document}"
    )


class CodexWriter:
    def __init__(self, model, codex_bin, codex_home=None, timeout=180):
        self.model, self.timeout = model, timeout
        self.binary = shutil.which(codex_bin)
        if not self.binary:
            raise ValueError("Codex binary not found")
        self.connection = ConnectionConfig(codex_home)

    def call(self, role, payload, schema, directory):
        workspace = directory / "workspace"
        shutil.copytree(ROOT / "skills" / role, workspace / ".agents" / "skills" / role)
        shutil.copy2(ROOT / "template.tex", workspace / "template.tex")
        atomic_json(workspace / "input.json", payload)
        atomic_json(directory / "schema.json", schema.model_json_schema())
        prompt = f"Use ${role}. Read .agents/skills/{role}/SKILL.md, input.json and template.tex. Return only the schema-constrained result."
        atomic_text(directory / "prompt.txt", prompt)
        argv = [
            self.binary,
            "exec",
            "--ignore-user-config",
            *self.connection.arguments,
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            self.model,
            "--json",
            "--cd",
            str(workspace),
            "--output-schema",
            str(directory / "schema.json"),
            "--output-last-message",
            str(directory / "result.json"),
            "-c",
            'approval_policy="never"',
            "-c",
            'web_search="disabled"',
            "-c",
            'model_reasoning_effort="high"',
            "-",
        ]
        atomic_json(
            directory / "command.json",
            {"argv": self.connection.redact(argv), "connection": self.connection.identity},
        )
        with (
            (directory / "events.jsonl").open("w") as out,
            (directory / "stderr.log").open("w") as err,
        ):
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
                text=True,
                env={**os.environ, "CODEX_HOME": str(self.connection.home)},
                start_new_session=True,
            )
            try:
                communicate_with_quota_detection(process, prompt, self.timeout, directory)
            except BaseException:
                stop_process(process)
                raise
        if process.returncode:
            raise RuntimeError(f"Report {role} failed; inspect {directory}")
        return schema.model_validate(read_json(directory / "result.json"))


def write_report(source_dir, output_dir, backend):
    source_dir, output_dir = Path(source_dir).resolve(), Path(output_dir).resolve()
    if output_dir.is_relative_to(source_dir) or source_dir.is_relative_to(output_dir):
        raise ValueError("Choose a separate output directory outside the verification run")
    with run_lock(output_dir):
        if (output_dir / "status.json").exists():
            raise ValueError("Use a fresh report output directory")
        atomic_json(output_dir / "status.json", {"status": "running"})
        try:
            source = source_snapshot(source_dir / "report.json")
            atomic_json(output_dir / "source.json", source)
            provenance = {
                "source_directory": str(source_dir),
                "source_sha256": hashlib.sha256(
                    json.dumps(source, sort_keys=True).encode()
                ).hexdigest(),
                "assets": {
                    str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in ROOT.rglob("*")
                    if p.is_file() and p.suffix in (".py", ".md", ".tex")
                },
            }
            atomic_json(output_dir / "provenance.json", provenance)
            draft = backend.call(
                "write-referee-report", {"source": source}, Draft, output_dir / "writer"
            )
            validate_draft(source, draft)
            atomic_json(output_dir / "draft.json", draft.model_dump())
            audit = backend.call(
                "check-referee-report",
                {"source": source, "draft": draft.model_dump()},
                Fidelity,
                output_dir / "editorial-check",
            )
            atomic_json(output_dir / "editorial-check.json", audit.model_dump())
            if not audit.approved:
                raise ValueError(
                    "Editorial fidelity check withheld the report; see editorial-check.json"
                )
            atomic_text(output_dir / "report.tex", render(source, draft))
            atomic_json(
                output_dir / "status.json", {"status": "completed", "editorial_check": "approved"}
            )
        except BaseException as exc:
            atomic_json(
                output_dir / "status.json",
                {"status": "failed", "error": type(exc).__name__ + ": " + str(exc)},
            )
            raise
    return output_dir / "report.tex"


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Draft a template-based report from released verification results"
    )
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--codex-bin", default="codex")
    p.add_argument("--codex-home", type=Path)
    p.add_argument("--timeout", type=float, default=180)
    args = p.parse_args(argv)
    try:
        result = write_report(
            args.run_dir,
            args.output_dir,
            CodexWriter(args.model, args.codex_bin, args.codex_home, args.timeout),
        )
        print(result)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
