# PDF-only paper review

Use this path for mathematical papers without a verified corresponding native
Markdown/LaTeX source. Never route existing native-source runs through OCR. Preserve
original PDFs and all prior runs; do not change installed snapshots used by active
runs. Runs started with the former `prepare-pdf`/`verify-pdf` commands keep using them.

## Review a PDF

```bash
uv run --script /absolute/skill/scripts/osverify.py verify \
  --review-type paper --paper /absolute/input/paper.pdf --decompose \
  --codex-home /absolute/account --run-dir /absolute/run
```

The engine first transcribes every physical page into Markdown/LaTeX with a fresh
Codex visual call and independently checks it against the original pixels, in
`/absolute/run/pdf-input/`; it then reviews the approved transcription. Poppler
(`pdftoppm`) is required; Pillow is a declared dependency. No separate OCR service
or account is used. All pages, including publisher covers, have explicit page/line
mapping, and page images and detail crops stay frozen for inspection.

A corrected draft receives another independent check before approval (up to 3 per
page). Unreadable content or exhausted checks blocks formal review. Inspect
`pdf-input/status.json` and the retained page attempts; never claim that a schema-valid
page approval establishes error-free OCR. Do not edit the frozen `paper.md` or
`ocr-manifest.json`.

To inspect the transcription before reviewing, run it alone and then reuse it:

```bash
uv run --script /absolute/skill/scripts/osverify.py ocr \
  --pdf /absolute/input/paper.pdf --run-dir /absolute/run-ocr \
  --codex-home /absolute/account
uv run --script /absolute/skill/scripts/osverify.py verify \
  --review-type paper --paper /absolute/input/paper.pdf --ocr-dir /absolute/run-ocr \
  --decompose --codex-home /absolute/account --run-dir /absolute/run
```

Without `--question`, PDF papers use the same
automatic scope selection as native papers (`--targets smart|lazy|full`; `manual` needs
LaTeX labels and is unavailable for PDFs). Defaults
are xhigh effort, live search, 16 concurrent verifier calls and no early rejection stop;
`--decompose` enables segment review. `--early-stop`, `--max-attempts`, `--max-issues`,
`--timeout-seconds` and `--no-report` are explicit overrides. Quota exhaustion stops
the invocation; resume only at user request with the same command.

Before independent issue validation and again before feedback audit/release, a fresh
source checker inspects original pages for every cited paper span and supporting
assumptions/context. Additional pages and detail views remain available. Source
fidelity approval permits mathematical validation; it does not confirm the defect.

An OCR mismatch or unreadable source blocks the affected issue chain, produces an
incomplete review, and never becomes an author-facing mathematical diagnosis or a
clarification request blaming the paper. Source-check JSON and failure artifacts
remain internal. If a later mismatch is found, preserve the preparation and old
review, reprepare the PDF in a new directory, and start a fresh review. Do not patch
frozen input or reuse judgments based on the old transcription. Current conservative
implementation reruns the full review, including affected downstream dependencies.

Read report.json/report.md for released exact feedback and actual completion/coverage.
Check .verification/jobs/*/attempt-*/ocr-source/source-check.json for input blockers.
Do not deliver a clean handoff for incomplete OCR/source checks. Reports require the
same local LaTeX tooling as text reviews; no-report preserves the review artifacts.
