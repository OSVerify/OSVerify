from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backend import DEFAULT_MODEL, CodexBackend, available_models
from .cli_options import ExplicitOption
from .demo import DemoBackend, demo_request
from .exporting import export_report
from .models import (
    FeedbackAudit,
    IssueReview,
    ProposalBatch,
    ReviewDiagnostics,
    ReviewReport,
    ReviewRequest,
)
from .pipeline import RunConfig, Verifier
from .storage import atomic_json, atomic_text, read_json
from .workflow import DecomposingVerifier, WorkflowConfig
from .workflow_models import DuplicateAudit, DuplicatePlan, TextDecomposition, WorkflowReport


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="osverify", description="OSVerify: one-sided review of solutions and papers with audited feedback")
    commands = root.add_subparsers(dest="command", required=True)
    verify = commands.add_parser(
        "verify", help="Review a solution (question + answer) or a paper through Codex"
    )
    inputs = verify.add_mutually_exclusive_group()
    inputs.add_argument("--request", type=Path, help="ReviewRequest JSON")
    inputs.add_argument(
        "--question", type=Path,
        help="Plain text/Markdown question; for a paper, an optional review scope",
    )
    verify.add_argument(
        "--solution", "--paper", dest="solution", type=Path, metavar="PATH",
        help="Plain text/Markdown solution; with --review-type paper, the paper (.tex, .md or .pdf)",
    )
    verify.add_argument(
        "--targets", choices=["smart", "lazy", "manual", "full"],
        help="Paper without --question: results to review (default smart)",
    )
    verify.add_argument(
        "--labels", nargs="+", default=[], help="With --targets manual: LaTeX \\label names to review"
    )
    verify.add_argument(
        "--ocr-dir", type=Path, help="PDF paper: reuse a transcription made by `osverify ocr`"
    )
    verify.add_argument("--request-id", default="review")
    verify.add_argument("--review-type", "--review-kind", dest="review_kind", choices=["solution", "paper"], default="solution", help="Review a solution or a paper (--review-kind is a compatibility alias)")
    verify.add_argument("--early-stop", action="store_true", help="Paper only: stop after a confirmed blocking issue")
    verify.add_argument("--mode", choices=["full_solution", "partial"], help="Solution review: full answer (default) or partial progress against the original question")
    verify.add_argument(
        "--proposals", type=Path, help="Optional existing ProposalBatch JSON; skip proposer"
    )
    verify.add_argument("--run-dir", type=Path, required=True)
    verify.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Codex model (default {DEFAULT_MODEL}; `osverify models` lists the account's models)"
    )
    verify.add_argument(
        "--codex-bin", default="codex", help="Select a compatible CLI, including a bundled binary"
    )
    verify.add_argument(
        "--codex-home",
        type=Path,
        help="Use a labeled CLI account without switching the desktop account",
    )
    # Explicit values also apply to paper severity classification and report writing.
    verify.add_argument("--reasoning-effort", default="xhigh", action=ExplicitOption)
    verify.add_argument("--timeout-seconds", type=float, default=900, action=ExplicitOption)
    verify.set_defaults(explicit_options=frozenset())
    # Mathematics only for now; other paper fields are not offered.
    verify.set_defaults(field=None, paper_image=[], paper_pdf=None)
    verify.add_argument("--web-search", choices=["disabled", "cached", "live"], default="live")
    verify.add_argument(
        "--workers",
        type=int,
        default=16,
        help="Maximum concurrent verifier calls across all stages (default: 16)",
    )
    verify.add_argument(
        "--max-issues",
        type=int,
        default=None,
        help="Optional whole-solution issue count cap; omitted means no count cap",
    )
    verify.add_argument("--no-report", action="store_true", help="Skip automatic reader/PDF reports (severity classification still runs)")
    verify.add_argument("--max-attempts", type=int, default=2)
    verify.add_argument(
        "--decompose",
        action="store_true",
        help="Review root and one original-text decomposition layer with global priority scheduling",
    )
    verify.add_argument(
        "--no-early-stop",
        action="store_true",
        help="With --decompose, continue after audited rejection; default stops and cancels remaining work",
    )
    demo = commands.add_parser(
        "demo", help="Run a scripted, offline demonstration (no Codex calls)"
    )
    demo.add_argument("--run-dir", type=Path, required=True)
    schemas = commands.add_parser("schemas", help="Export the versioned JSON contracts")
    schemas.add_argument("--out", type=Path, required=True)
    export = commands.add_parser("export", help="Export an existing report without model calls")
    export.add_argument("--run-dir", type=Path, required=True, help="Verification run or Rethlas reviews/<key> directory")
    export.add_argument("--format", choices=["latex", "markdown"], default="latex")
    export.add_argument("--output", type=Path, required=True)
    models = commands.add_parser("models", help="List the Codex models you can select with --model")
    models.add_argument("--codex-home", type=Path, help="Codex account directory (default: $CODEX_HOME or ~/.codex)")
    # Dispatched before parsing (see main); listed here for `osverifier --help`.
    commands.add_parser("ocr", add_help=False, help="Checked page-by-page transcription of a PDF paper")
    commands.add_parser("report", add_help=False, help="Draft a referee report from a finished review")
    return root


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["ocr"]:
        from osverifier_reports.ocr import ocr_main

        return ocr_main(argv[1:])
    if argv[:1] == ["report"]:
        from osverifier_reports.writer import main as report_main

        return report_main(argv[1:], prog="osverify report")
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "models":
            for model in available_models(arguments.codex_home):
                print(model + ("  (default)" if model == DEFAULT_MODEL else ""))
            return 0
        if arguments.command == "export":
            output = arguments.output.resolve()
            if output.exists():
                raise ValueError("Output already exists; choose a new path to preserve existing artifacts")
            text, _, _ = export_report(arguments.run_dir, arguments.format)
            atomic_text(output, text)
            print(output)
            return 0
        if arguments.command == "schemas":
            from osverifier.models import PartialFeedbackAudit, PartialIssueReview
            from osverifier_reports.paper import PaperReport

            for model in (
                ReviewRequest,
                ProposalBatch,
                IssueReview,
                FeedbackAudit,
                ReviewReport,
                ReviewDiagnostics,
                TextDecomposition,
                DuplicatePlan,
                DuplicateAudit,
                WorkflowReport,
                PaperReport,
                PartialIssueReview,
                PartialFeedbackAudit,
            ):
                atomic_json(
                    arguments.out / f"{model.__name__}.schema.json", model.model_json_schema()
                )
            return 0
        if arguments.command == "demo":
            report = Verifier(DemoBackend()).verify(demo_request(), arguments.run_dir)
        else:
            automatic = (arguments.targets, arguments.labels, arguments.ocr_dir)
            if arguments.review_kind != "paper" and any(automatic):
                raise ValueError("--targets, --labels and --ocr-dir apply only to --review-type paper")
            if arguments.review_kind == "paper" and not arguments.request:
                from osverifier_reports.paper_inputs import prepare_paper_review

                exit_code = prepare_paper_review(arguments)
                if exit_code is not None:
                    return exit_code
            if not arguments.request and not arguments.question:
                raise ValueError("--question (or --request) is required for solution review")
            if arguments.request:
                if arguments.solution:
                    raise ValueError("--solution cannot be combined with --request")
                request = ReviewRequest.model_validate(read_json(arguments.request))
                if arguments.mode is not None and arguments.mode != request.mode:
                    raise ValueError("--mode conflicts with the request JSON")
            else:
                if not arguments.solution:
                    raise ValueError("--solution is required with --question")
                request = ReviewRequest(
                    request_id=arguments.request_id,
                    question=arguments.question.read_text(encoding="utf-8"),
                    candidate_solution=arguments.solution.read_text(encoding="utf-8"),
                    mode=arguments.mode or "full_solution",
                )
            if request.mode == "partial" and arguments.review_kind != "solution":
                raise ValueError("--mode partial is supported only with --review-type solution")
            if arguments.review_kind == "solution" and (arguments.run_dir / "paper-task.json").exists():
                raise ValueError("This is a paper run; resume with --review-type paper")
            if arguments.early_stop and (arguments.review_kind != "paper" or arguments.no_early_stop):
                raise ValueError("--early-stop is paper-only and conflicts with --no-early-stop")
            if arguments.review_kind == "paper" and arguments.proposals:
                raise ValueError("Paper review does not accept --proposals")
            backend = CodexBackend(
                model=arguments.model,
                codex_bin=arguments.codex_bin,
                codex_home=arguments.codex_home,
                reasoning_effort=arguments.reasoning_effort,
                timeout_seconds=arguments.timeout_seconds,
                web_search=arguments.web_search,
            )
            proposals = (
                ProposalBatch.model_validate(read_json(arguments.proposals))
                if arguments.proposals
                else None
            )
            if arguments.review_kind == "paper":
                from osverifier_reports.paper import run_paper

                report = run_paper(arguments, request, backend)
            elif arguments.decompose:
                if proposals is not None:
                    raise ValueError(
                        "--proposals is only available for the legacy root-only workflow"
                    )
                report = DecomposingVerifier(
                    backend,
                    WorkflowConfig(
                        workers=arguments.workers,
                        max_issues=arguments.max_issues,
                        max_attempts=arguments.max_attempts,
                        early_stop_on_reject=not arguments.no_early_stop,
                    ),
                ).verify(request, arguments.run_dir)
            else:
                if arguments.no_early_stop:
                    raise ValueError("--no-early-stop requires --decompose")
                report = Verifier(
                    backend,
                    RunConfig(
                        workers=arguments.workers,
                        max_issues=arguments.max_issues,
                        max_attempts=arguments.max_attempts,
                    ),
                ).verify(request, arguments.run_dir, proposals=proposals)
        is_paper = arguments.command == "verify" and arguments.review_kind == "paper"
        is_ai = is_paper and arguments.field == "ai"
        if not is_paper:
            print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        if arguments.command == "verify" and not is_ai and (report.run_status == "completed" or (is_paper and not report.interruption_reason)) and not arguments.no_report and getattr(report, "classification_status", None) != "failed":
            from osverifier_reports.automatic import after_verify

            after_verify(arguments)
        if is_paper:
            public_report = read_json(arguments.run_dir / "report.json")
            print(json.dumps(public_report, ensure_ascii=False, indent=2))
            if is_ai and not arguments.no_report and not public_report.get("interruption_reason"):
                from osverifier_reports.automatic import after_ai_review

                after_ai_review(arguments.run_dir)
            if public_report.get("interruption_reason") == "quota_exhausted":
                return 2
        return 0 if report.run_status == "completed" else 2
    except KeyboardInterrupt:
        print("Interrupted. Resume with the same inputs and run directory.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
