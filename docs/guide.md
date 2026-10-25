# OSVerify guide

Use OSVerify to review a mathematical solution or paper and read the resulting
audited feedback. For an overview or agent-skill installation, see the
[README](../README.md).

- [Install and try the demo](#install-and-try-the-demo)
- [Review a solution](#review-a-solution)
- [Review a paper](#review-a-paper)
- [Read and export results](#read-and-export-results)
- [Choose models and options](#choose-models-and-options)
- [Interrupt and resume](#interrupt-and-resume)
- [Troubleshooting](#troubleshooting)

## Install and try the demo

Install Python 3.12+, [uv](https://docs.astral.sh/uv/), and an authenticated
[Codex CLI](https://github.com/openai/codex) 0.162 or newer. PDF inputs also require
Poppler; generating referee-report PDFs requires XeLaTeX or LuaLaTeX.

```bash
git clone https://github.com/OSVerify/OSVerify && cd OSVerify
uv sync
uv run osverify demo --run-dir runs/demo
```

Open `runs/demo/report.md` to see a sample result. This is a scripted, offline
walkthrough with no model calls; it demonstrates the output format and feedback
audit, not mathematical review quality.

The examples below run from the checkout and use your signed-in Codex account.
For a graphical interface, use the separate
[Rethlas-OSV desktop app](https://github.com/OSVerify/Rethlas-OSV).

## Review a solution

Save the original question in `question.md` and the candidate solution in
`solution.md`, then run:

```bash
uv run osverify verify --question question.md --solution solution.md \
  --decompose --run-dir runs/my-solution
```

`--decompose` reviews the whole solution and its segments, retaining the full
context for each. It uses one segmentation layer. Omit the flag for whole-solution
review alone. OSVerify leaves the input files unchanged.

### Check unfinished progress

Keep the original question and supply the current partial argument:

```bash
uv run osverify verify --question question.md --solution progress.md \
  --mode partial --decompose --run-dir runs/my-progress
```

Explicit pending obligations and provisional assumptions may remain open; claims
that a result has already been established are still checked. A clean partial
review returns `PASS`, meaning you can continue working. A partial `REJECT`
concerns a demonstrated obstruction to the current route, not the original
problem. Partial mode is available for solutions only.

### Supply reference files

When the solution cites a source that web search cannot retrieve, such as an old
paper you found yourself, put the files in a directory and pass it:

```bash
uv run osverify verify --question question.md --solution solution.md \
  --references refs/ --decompose --run-dir runs/my-solution
```

The proposer, issue validator and feedback auditor can list and read these files.
PDFs (scans through their page images, which needs Poppler), PNG/JPEG images and
UTF-8 text such as Markdown or LaTeX are readable. A supplied file is a source to
compare, not proof: reviewers check that it is the cited work, then read the
statement, its hypotheses and how the solution applies it. A file that is unreadable
or does not contain the cited result leaves the citation uncertain. The files are
copied into the run directory and become part of its inputs, so adding, changing or
removing one needs a new run directory (macOS `.DS_Store` and `._*` files are ignored). Paper review does not accept `--references`.

## Review a paper

Pass a LaTeX or Markdown paper:

```bash
uv run osverify verify --review-type paper --paper paper.tex \
  --decompose --run-dir runs/my-paper
```

By default, OSVerify selects the main theorems and the key steps they rely on.
Paper review reports findings and coverage, without an overall solution verdict.

### Choose the review scope

When no `--question` is supplied, `--targets` controls the selection:

| Value | Scope |
| --- | --- |
| `smart` (default) | A model selects the main theorems and key supporting steps. |
| `lazy` | Results stated in the introduction. |
| `manual` | Theorem-like environments with the LaTeX labels you specify; selection uses no model call. |
| `full` | The whole paper. |

```bash
uv run osverify verify --review-type paper --paper paper.tex \
  --targets manual --labels thm:main lem:key --decompose --run-dir runs/paper-main

uv run osverify verify --review-type paper --paper paper.tex \
  --targets full --decompose --run-dir runs/paper-full
```

To write your own scope, put it in `scope.md` and pass `--question scope.md`
instead of `--targets`. Selected statements are preserved as verbatim excerpts.
Coverage in the report refers to this selected scope.

### Use a PDF

Prefer a verified LaTeX or Markdown source when available. Otherwise pass the PDF
directly; OSVerify transcribes and independently checks its pages before review:

```bash
uv run osverify verify --review-type paper --paper paper.pdf \
  --decompose --run-dir runs/my-pdf
```

To inspect the transcription first and reuse it later:

```bash
uv run osverify ocr --pdf paper.pdf --run-dir runs/paper-ocr
uv run osverify verify --review-type paper --paper paper.pdf \
  --ocr-dir runs/paper-ocr --decompose --run-dir runs/paper-review
```

No external OCR account is required. Suspected issues are checked against the
original pages before validation and again before feedback release. Unreadable
pages or transcription mismatches can block review; they are not paper errors.

### Cited references

Paper reviewers can retrieve public literature and inspect source passages or PDF
pages. Unchecked dependencies appear as review limitations. Access failures,
including login or subscription restrictions, do not establish an error in the
paper. See [Literature tools](literature-tools.md) for retrieval and evidence details.

## Read and export results

Start with `report.md` in the run directory. `report.json` contains the structured
result. Read the review status and coverage alongside the feedback: an unfinished
review may already contain useful findings.

### Solution decisions

| Decision | Full solution | Partial progress |
| --- | --- | --- |
| `READY_FOR_HUMAN` | Finished with nothing outstanding; send for human confirmation. | Not used. |
| `PASS` | Not used. | Finished with nothing outstanding; continue developing the solution. |
| `REVISE` | Address the released corrections, omissions or clarification requests. | Address the released feedback before continuing. |
| `REJECT` | An audited blocking error or demonstrated missing core obligation warrants rejection. | An audited error establishes a fundamental obstruction to the current route. |

`REJECT` takes precedence over `REVISE`. A confirmed error with a justified local
repair uses `REVISE`; a repair request alone does not guarantee that a repair
exists. Failure to find a repair is insufficient grounds for rejection.

`run_status` is `running`, `completed`, or `incomplete`. A solution review with no
usable decision yet has `decision=null`. Incomplete reviews cannot produce
`READY_FOR_HUMAN` or `PASS`; already audited revision or rejection feedback can
still be available. Passing review does not certify mathematical correctness.

With `--decompose`, an intentional rejection stop can have `run_status=completed`
and `coverage_complete=false`: the workflow stopped as configured, leaving some
scopes unreviewed. See [stopping options](#choose-models-and-options).

### Paper findings

Paper reports show review status, selected-scope coverage and findings, with no
overall `PASS`, `READY_FOR_HUMAN`, `REVISE` or `REJECT` label. Findings are grouped
using OSVerify's repair-based classification:

| Group | Meaning |
| --- | --- |
| Major | A confirmed defect without a complete fix in the released feedback. |
| Minor | The released feedback supplies a concrete, complete fix that preserves the paper's statement. |
| Unclassified | Clarification-only findings, uncertain fix completeness, or withheld classification. |

These labels describe the available fix, rather than the importance of the theorem.
Classification is audited separately; `classification_status` records its outcome.

### Referee PDFs and offline exports

`verify` normally drafts and checks a referee report, then compiles a PDF. The CLI
prints the PDF path to stderr and keeps structured JSON on stdout. Automatic
reports are stored beside the run:

```text
runs/my-paper/report.md
runs/my-paper/report.json
runs/my-paper-reports/<report-hash>/<format-version>/report.pdf
```

PDF reporting uses additional model calls. Add `--no-report` to skip it; paper
issue classification still runs. The review remains available if PDF generation
fails. See [Troubleshooting](#troubleshooting) for recovery.

To export the saved feedback as Markdown or LaTeX without model calls:

```bash
uv run osverify export --run-dir runs/my-solution --format markdown --output report-copy.md
uv run osverify export --run-dir runs/my-solution --format latex --output report.tex
```

Choose a new output path; export refuses to overwrite files. LaTeX export needs
no TeX installation until you compile it, for example with
`xelatex -no-shell-escape report.tex`. Custom math macros or fonts may need local
definitions. Export preserves any incomplete-review notice.

To draft a referee report separately after a review has stopped, use a fresh
output directory outside the run:

```bash
uv run osverify report --run-dir runs/my-solution \
  --output-dir report-drafts/my-solution
```

This calls a writer and an independent editorial checker. It saves approved
`report.tex`; compile that file separately. It does not rerun verification.
The [runtime reference](runtime-reference.md#report-generation) describes report
audits, retries and artifacts.

## Choose models and options

List the models available to your Codex account:

```bash
uv run osverify models
```

The following options apply to `verify`; use `uv run osverify <command> --help`
for options on other commands.

| Option | Default and use |
| --- | --- |
| `--model MODEL` | `gpt-6-astra`; select another available model. |
| `--workers N` | `16`; maximum concurrent verifier calls. Lower it to reduce concurrency. |
| `--reasoning-effort LEVEL` | `xhigh`; select an effort supported by your model. |
| `--web-search MODE` | `live`; also accepts `cached` or `disabled`. |
| `--timeout-seconds N` | `900` per verifier attempt. An explicit value also applies to paper classification and report writing. |
| `--max-attempts N` | `2` attempts per verifier job per invocation. |
| `--timeout-retries N` | `1`. A verifier attempt that times out earns one more attempt, up to N per job per invocation; once a job has timed out, its later attempts (also after a resume) run with doubled time limits. |
| `--max-issues N` | No cap by default; applies to each proposer, including each segment. |
| `--references DIR` | None; solution review only. Reference files the reviewers may read, frozen with the run (see [Supply reference files](#supply-reference-files)). |
| `--codex-bin PATH` | `codex` on your PATH; choose a different compatible binary. |
| `--codex-home PATH` | The selected CLI account directory; defaults to `$CODEX_HOME` or `~/.codex`. |
| `--request-id NAME` | Label the review; automatic paper preparation otherwise uses the paper title. |
| `--no-report` | Skip automatic referee-report generation. |

Concurrency, timeouts and issue limits do not impose a hard token budget. A cap or
unavailable evidence that leaves planned search unfinished is recorded as incomplete.
For call accounting and provider configuration, see the
[runtime reference](runtime-reference.md).

With `--decompose`, solution review stops after an audited rejection by default;
add `--no-early-stop` to continue inspecting other scopes. Paper review continues
by default; add `--early-stop` to stop after a confirmed blocking finding.
`REVISE` does not trigger early stopping.

## Interrupt and resume

Use Ctrl+C to interrupt. Resume by rerunning the identical command with the same
run directory. Completed, validated jobs are reused; unfinished jobs receive fresh
attempts. A completed rejection-stop run keeps its original partial coverage.

Inputs (including `--references` files), mode, model/account, configuration and
engine version must match the saved run. Use a new directory after changing them. Resume old source-based runs with
their original version. Agent-skill runs pin their engine in `.osverify-runtime/`;
see [historical runs](runtime-reference.md#historical-runs-and-skill-commands).

### Quota exhaustion

When the provider explicitly reports exhausted quota, OSVerify stops further model
calls and saves the available feedback and checkpoints. It does not wait for a
reset or restart automatically. Once quota recovers, repeat the same command.
Ordinary rate limits and network errors use bounded retries.

Paper review also saves a partial report containing already audited feedback and
classifications. With a working LaTeX engine, it can compile a partial PDF locally
without model calls. The report identifies the interrupted phase, including cases
where verification finished but report writing did not. `--no-report` skips this
PDF too; public JSON and Markdown are still saved.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| CLI or model unavailable | Check that your Codex CLI is signed in and compatible; use `models`, `--model` or `--codex-bin` as needed. |
| PDF transcription cannot start | Install Poppler and check that the input PDF is readable. |
| Transcription or original-page checks fail | Inspect the saved transcription/source-check artifacts. Preserve the old run; prepare corrected input in a new run. |
| Review exists but there is no PDF | Read the separate report status and compilation log. Install XeLaTeX or LuaLaTeX if missing, then repeat the command. Verification caches are reused; report retries can make additional model calls. |
| Paper classification failed | Findings remain in `report.md`; rerun the same command to retry classification. |
| Resume reports changed inputs/settings | Return to the original inputs and engine, or start a new run directory. |
| No findings, but review is incomplete | Inspect status, coverage and diagnostics; absence of findings does not establish correctness. |

For `verify`, exit code `0` means the review completed (including a rejection),
`2` means incomplete review or paper quota interruption, `1` means a setup/contract
failure, and `130` means interruption. Check report status separately from PDF
generation status.

For integration and debugging, see the [output contract](output-contract.md),
[runtime reference](runtime-reference.md) and [literature tools](literature-tools.md).
