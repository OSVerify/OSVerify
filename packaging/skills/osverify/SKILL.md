---
name: osverify
description: Review mathematical solutions and papers with OSVerify independent issue and exact-feedback audits; PDF-only papers include checked transcription and original-page source checks. Use for proof checking or paper correctness review.
---

# OSVerify

Use the bundled Python engine to review supplied material. Do not substitute your own
single-session judgment for the engine's independently audited output. This skill
reviews existing text; it does not generate or revise solutions.

## Route the request

- **Solution:** read [solution.md](references/solution.md). Use `full_solution` for a
  submitted complete answer and `partial` for unfinished progress. Keep the complete
  original question in both modes. Ask only if the intended completeness is unclear.
- **PDF-only paper:** read [pdf.md](references/pdf.md). Prefer a verified corresponding Markdown/LaTeX source when available; use the opt-in PDF path only when transcription is needed.
- **Paper referee with native text:** read [paper.md](references/paper.md). Review mathematical correctness within an explicit selected scope. This is not novelty scoring,
  venue acceptance advice, or a complete editorial review.

## Runtime

Resolve `scripts/osverify.py` relative to this SKILL.md; never hardcode a repository
or account path. Python engine files and role instructions are bundled under
`scripts/review-runtime/`, including literature tools for native and checked-PDF
paper review. Read [literature.md](references/literature.md) when reviewing papers
with external dependencies. Use `uv run --script /absolute/skill/scripts/osverify.py ...`;
its script metadata declares Python >=3.12 and runtime dependencies. `uv` may fetch
missing dependencies on first use. An existing Python environment with those
packages can instead run the script directly. An authenticated compatible Codex CLI
must be available; inspect `--help` when additional flags are needed.

Use the model the user asks for with `--model`; otherwise omit it to use the default
`gpt-6-astra`. `osverify.py models` lists the models the selected account offers.
Pass a chosen account through `--codex-home`; never log in, switch the desktop account,
or copy credentials. Defaults are xhigh reasoning, live search, 16 concurrent verifier calls,
and no issue count cap. Preserve explicit overrides.

Save input snapshots and outputs in a fresh user-workspace run directory, outside
the installed skill. Preserve original wording, hypotheses, formulas, and source
order. Keep the original files unchanged. Run the engine and continue tracking it
until completion, user cancellation, or an actionable execution failure. A review
can be long; communicate meaningful progress without restarting it.

New runs pin a checked engine copy in `.osverify-runtime/`; later invocations use
that copy even after a skill update. Do not edit or refresh it. Older runs without
this pin must use their original engine. The previous skill snapshots remain under
`scripts/runtime/` (native) and `scripts/ocr-runtime/` (PDF); select these with
`osverify.py --legacy-runtime COMMAND ...` only for runs made by those snapshots.
The command line matches `osverifier`: `verify` (solutions and papers, including PDF),
`ocr`, `report`, `export`. Runs started with the former `prepare-paper`, `prepare-pdf`
or `verify-pdf` commands keep using those commands with their pinned engine.
For other historical source snapshots, replay their original command. Never migrate
old caches or overwrite frozen runs to enable new features; start a fresh directory.

## Read the result

Read `report.json` and `report.md`, including run status, coverage, interruption and
classification fields. Only release the exact approved feedback text; do not expose
proposer allegations or add unaudited repairs to the report. Link the saved report
and disclose the actual scope and any unfinished work. Diagnostic artifacts and
trajectory are available for troubleshooting, not substitute public verdicts.

An exit code, valid schema, offline demo, or absence of findings does not prove
mathematical correctness. Partial PASS only permits continuation; completed full
READY_FOR_HUMAN only permits final human confirmation. Paper reports have no overall
solution verdict. Audited-rejection early stopping may complete the configured
workflow while leaving coverage incomplete.

On quota exhaustion stop without further dispatch. Preserve checkpoints. Resume
only when the user requests it, replaying the same inputs, model/account, flags,
run directory and bundled engine. Changed source, policy or configuration requires
another run. Ctrl+C uses the engine's normal cancellation path; avoid force killing.
Report rendering has its own status; a failed PDF does not erase a saved review.

For mathematical referee results, deliver the generated reader-facing PDF as the
primary report, following [paper.md](references/paper.md). Keep report.json/report.md
as detailed internal records. Use the classic LaTeX report with Review summary,
status table, Major/Minor issues, source quotations and concise reference notes.
Formula typography and printed paper locators require editorial fidelity approval;
never rewrite the underlying mathematical feedback. Quota snapshots use the same
layout but explicitly mark unfinished review and editorial work without model calls.
