# OSVerify guide

Detailed reference for the `osverify` command line. Start with the [README](../README.md).

## Workflows

For standalone `osverify verify`, use `--decompose` for parallel review. The root
proposer and one verbatim segmentation pass start together. Once segmentation
finishes, segment proposers join the same global queue; each finished proposal
batch proceeds through audited deduplication and issue review without waiting for
other scopes. Segment count is selected by the model, and full source/search
access is retained. An independently audited rejection can stop and cancel remaining
work. Without `--decompose`, the legacy whole-solution workflow remains available.
Solution review also supports `--mode partial` against the original question.

## The feedback contract

A real issue does not make every proposed repair correct. The issue validator first
checks the allegation and prepares separate feedback candidates. A fresh feedback
auditor checks the finding again and audits each exact text: diagnosis, repair
request, repair suggestion, or clarification request.

Only approved text is released, verbatim. The auditor may withhold text or dispute
the finding, but cannot publish its own rewrite. Internal rationale, unapproved
proposer prose, and withheld suggestions never enter the integration report. There
is no model-written summary after this gate. See [the output contract](output-contract.md)
and the [shared mathematical policy](../src/osverifier/skills/policy.md).

Solution-mode public reports have three decisions (paper review has no overall verdict):

| Decision | Meaning |
| --- | --- |
| `READY_FOR_HUMAN` | The review finished with no outstanding issue or revision feedback; hand off for final human confirmation. |
| `REVISE` | Apply audited feedback for supported local repairs, local omissions, minor corrections, or justified clarification requests. |
| `REJECT` | A confirmed blocking error or demonstrated missing core obligation warrants rejection under the repair policy; the auditor upholds the basis and diagnosis. |

`REJECT` takes precedence over `REVISE` across different issues. A confirmed error with
a supported local repair uses `REVISE`: the correction must be justified, preserve
the original question, and reconnect affected downstream uses within the existing
proof route. The original error remains confirmed; the auditor checks this decision
as well as the feedback. Full-solution rejection can also address a demonstrably
uncompleted core obligation: identify the required result and why the full submitted
argument does not supply it. Missing routine detail, legitimate implicit steps, and
failure to find a repair do not establish that basis. A supported local completion
uses revision; an already justified step needs no correction. A repair request does
not certify repairability. Human readiness is a workflow judgment, not
a formal proof certificate or a measured guarantee of correctness.

Execution/audit completion is separate: `run_status` is `running`, `completed`, or
`incomplete`. An unfinished review without usable feedback has `decision=null`;
this is no decision yet, not a fourth outcome for the solution. Confirmed revision
or rejection feedback may already be returned while other work is unfinished.
Only a completed review may yield `READY_FOR_HUMAN`. A disputed diagnosis or an
unresolved issue without audited clarification cannot silently become human readiness.

In the decomposed workflow, an intentional audited-rejection stop has
`run_status=completed`, `termination_reason=audited_rejection`, and
`coverage_complete=false`: the configured workflow finished, while remaining work
was skipped or cancelled. All planned scopes must finish before human readiness.
`REVISE` continues traversal. Cancellation is confined to the current sample.

Detailed findings, missing-content flags, withheld advice, and execution errors are
stored in `diagnostics.json`. There is no public `completion`, `next_action`, or
`feedback_review_status` taxonomy to interpret. Withheld optional advice is simply
omitted; an approved diagnosis still supports `REVISE` or `REJECT`.

## Install and run

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required for development.

```bash
uv sync
uv run osverify demo --run-dir runs/demo
uv run osverify schemas --out schemas
uv run pytest
```

The demo is **scripted and offline**, with no model calls. It demonstrates an omitted
converse, approved repair feedback, and a withheld suggestion that would narrow the
question. It is not evidence of mathematical reviewing quality.

Use an authenticated, compatible Codex CLI for a real review:

```bash
uv run osverify verify \
  --question /absolute/path/question.md \
  --solution /absolute/path/candidate-solution.md \
  --request-id problem-001-v1 \
  --model YOUR_CODEX_MODEL_ID \
  --codex-bin /absolute/path/to/codex \
  --decompose \
  --run-dir runs/problem-001-v1
```

`--request` accepts a JSON request instead of the two text files. `--proposals` accepts
a `ProposalBatch` for the legacy root-only workflow and skips that proposer call.
Both workflows use the same mathematical issue-validation and exact-feedback gates.
The decomposed workflow stops on rejection by default; `--no-early-stop` explicitly
disables that behavior in `osverify verify --decompose` (and in Rethlas-OSV).
It continues the remaining issue and segment reviews while retaining any supported
`REJECT` decision. In Rethlas-OSV, `READY_FOR_HUMAN` still stops the experiment.
There is one decomposition layer, not recursive expansion.

`trajectory.jsonl` records nodes, attempts, cache hits, deduplication decisions,
issue decisions, early stops, failures, and cancellations. `trajectory.md` presents
those events for reading. Every job retains its exact input, schema, skill/policy,
command, response, and CLI-visible output/tool trace. `nodes/` preserves scopes and
verbatim excerpts; `candidates.json` and `aliases.json` preserve all allegations and
independently approved duplicate links. The final report releases approved feedback
verbatim. Completed runs verify a hashed completion checkpoint and return without
launching cancelled or skipped work on resume.

Optional `--codex-home /path/to/labeled/account` selects CLI authentication per
invocation; this tool does not run login or switch the desktop account. No API keys
are stored in this repository. The default model is `gpt-6-astra`; choose another with `--model` (`osverify models`
lists the account's models). CLI selection is explicit because binary
and account compatibility vary. Model capability is checked by the actual call,
not inferred from a successful CLI version command.

OSVerify reads connection settings from the selected `$CODEX_HOME/config.toml`
(`--codex-home`, then the environment, then `~/.codex`). It forwards only
`model_provider`, the selected provider's complete `model_providers` table,
`openai_base_url`, `chatgpt_base_url`, `cli_auth_credentials_store`,
`forced_login_method`, and `forced_chatgpt_workspace_id`. A default inline
`profile` / `profiles.<name>` selection is honored for these fields. Model selection
remains explicit through the existing CLI model options. Provider definitions retain
headers, environment-key references, query parameters, protocol and retry settings.
The original authentication home and inherited environment remain in use; credentials
are not copied and the user's configuration is not modified.

All other user config fields (including instructions, MCP servers, hooks, and feature
settings) are excluded. Both the verifier and the Rethlas-OSV generator retain
`--ignore-user-config` and receive the selected connection settings as explicit `-c`
overrides, followed by OSVerify's runtime settings. This isolates **user config
fields**, not every Codex discovery source: system/admin settings, separately discovered
skills, instructions, rules and installed integrations may still apply. Rethlas's own
memory MCP and agent settings remain explicitly configured.

Connection settings are frozen when the backend is constructed. Their source and
fingerprint enter resume provenance; changing them requires a new run directory.
Connection override values are redacted in `command.json`, including inline headers
and tokens. They are passed directly to the child process (no shell), so they may be
visible to local process inspection; prefer environment-backed credentials. Raw Codex
output/tool traces are retained as before and should be treated as private run data.
Environment credential values and auth-store contents are not fingerprinted, allowing
normal credential refresh. Named profile CLI selection and project-local connection
configuration are not supported by this adapter.

Codex runs use a read-only sandbox, schema-constrained output,
JSONL events, and a fresh conversation for every role/attempt. All roles default to
reasoning effort **`xhigh`** and **live web search**. Use search when checking theorem
hypotheses, references, or factual dependencies; the skills require primary sources
and actual source inspection rather than accepting snippets. Explicit
`--reasoning-effort` and `--web-search` overrides remain supported. Administratively
managed settings, installed integrations, and remote model behavior may still vary;
fresh conversations are context separation, not a statistical independence guarantee.

The CLI options are based on the official [non-interactive interface](https://learn.chatgpt.com/docs/non-interactive-mode)
and [repository skills](https://developers.openai.com/codex/skills), and on the locally
installed CLI's help. The runtime loads only the assigned packaged role skill, copies
the policy, and reads the immutable input inside an attempt workspace.

## Reviewing a paper

```bash
# LaTeX or Markdown: the main results and key steps are selected automatically (smart)
uv run osverify verify --review-type paper --paper paper.tex --decompose \
  --model YOUR_CODEX_MODEL_ID --run-dir runs/my-paper

# Choose the results by LaTeX label (no model call), or review the whole paper
uv run osverify verify --review-type paper --paper paper.tex --targets manual \
  --labels thm:main lem:key --decompose --model YOUR_CODEX_MODEL_ID --run-dir runs/my-paper-main
uv run osverify verify --review-type paper --paper paper.tex --targets full \
  --decompose --model YOUR_CODEX_MODEL_ID --run-dir runs/my-paper-full

# A hand-written review scope instead of automatic selection
uv run osverify verify --review-type paper --paper paper.tex --question scope.md \
  --decompose --model YOUR_CODEX_MODEL_ID --run-dir runs/my-paper-scope

# Only a PDF: transcribed with independent checks, then reviewed against the original pages
uv run osverify verify --review-type paper --paper paper.pdf --decompose \
  --model YOUR_CODEX_MODEL_ID --run-dir runs/my-pdf
```

`--targets` chooses what the review must establish when there is no `--question`:
**smart** (default) asks one fresh Codex call for the main theorems and the key steps
they rest on; **lazy** takes the results announced in the introduction; **manual**
extracts the theorem-like environments carrying `--labels`, deterministically and with
no model call; **full** reviews the whole paper. Every
selected statement must be a verbatim excerpt of the paper. The paper is copied into
`<run-dir>/paper-inputs/` (a PDF transcription into `<run-dir>/pdf-input/`) and reused
when the same command resumes; a changed paper needs a new run directory.
`osverify ocr --pdf paper.pdf --run-dir DIR --model ...` runs only the transcription, so
it can be inspected first and then reviewed with `--paper paper.pdf --ocr-dir DIR`.

## Desktop app and GUI

The local web GUI and the desktop app (**Rethlas-OSV**) live in
[OSVerify/Rethlas-OSV](https://github.com/OSVerify/Rethlas-OSV). They launch these same
command-line entry points as child processes, so every freezing, caching, quota and audit
rule described here applies unchanged.

## Export a report

The Rethlas-OSV run view keeps its rendered web report and offers **Download LaTeX (.tex)**
and **Download Markdown (.md)**, including individual Rethlas candidate reviews.
Downloads are available as soon as a released report exists. Incomplete reports retain
their partial-review warning and workflow coverage; exporting never marks a run complete.

The same offline exporter is available to terminal users and coding agents:

```bash
uv run osverify export --run-dir runs/problem-001-v1 --format latex --output report.tex
uv run osverify export --run-dir runs/problem-001-v1 --format markdown --output report-copy.md
# A specific Rethlas review:
uv run osverify export --run-dir runs/experiment/reviews/REVIEW_KEY --output review.tex
```

LaTeX uses the collaborator's `report_template.tex` layout (`amsart`, OSVerify author,
referee-report title, overall opinion, and issue-by-issue Location / Type / Concern /
Suggestion fields). It reads the public `report.json`, groups feedback by issue in
first-appearance order, and preserves each released text and audit reference.
This direct export is a detailed record; for the reader-facing mathematical-paper
report, use the automatic or separately drafted PDF described below.
The title uses the request ID; a paper review without `--request-id` is named after
the paper's own title. Counts refer to issues with released feedback, not
invented major/minor error counts. Public reports do not currently carry severity,
a separate evidence/check field, or an audited overall mathematical opinion, so the
export does not infer those fields or copy the template's illustrative claims.
Incomplete-review and partial-coverage notices remain visible. No internal rationale
or withheld feedback is read. Historical run artifacts are preserved unchanged.

Markdown export keeps the existing `report.md` snapshot, as does the web view.
Older directories containing only Markdown can still export an `amsart` document
with their original section structure. It makes no model calls and does not
resume or modify the review. The CLI refuses to overwrite an existing output file.
Markdown downloads retain the original source; LaTeX converts headings, lists,
emphasis, code, tables, and `$...$`, `$$...$$`, `\(...\)`, `\[...\]` math.
Links retain their labels and destinations as text. Raw HTML is displayed literally.
The standalone `.tex` uses standard packages and can be compiled with XeLaTeX or
LuaLaTeX, for example `xelatex -no-shell-escape report.tex`. Custom mathematical
macros may require definitions and non-Latin text may require a suitable font.
Offline export alone requires no LaTeX installation or Pandoc. The same downloads work in the desktop window.

## Automatic template PDF after verification

CLI `osverify verify` (including `--decompose`) and the GUI's **Verify a paper**
automatically draft the collaborator's `amsart` referee report and compile a PDF
when verification completes. Solution reporting adds up to two Codex calls using the same model
and selected account/provider settings. Verification prompts, audits, and decisions
are unchanged. Interrupted/incomplete verification does not start report generation.
The offline demo and internal Rethlas review loop do not trigger it.

Install **XeLaTeX or LuaLaTeX** (TeX Live/MacTeX) to produce PDFs; the desktop app
also needs this local dependency. UI results show **Download template PDF** and CLI
prints its path on stderr, preserving JSON stdout. Artifacts are stored outside the
run at `<run-name>-reports/<report-json-sha256>/<report-format-version>/`, with individual editorial attempts.
Existing Markdown/web reports remain available. PDF failure is shown separately and
does not change the verifier result or exit code; approved `.tex` and compilation
logs remain available. Rerunning the same CLI command reuses verifier caches and
approved report text, retrying only compilation if necessary. A completed PDF is reused.
Use `--no-report` to skip this postprocessing. Early rejection reports retain their
partial-coverage label. Compilation disables shell escape and restricts file access.

## Draft a referee report separately with Codex (optional)

**Draft template report (2 Codex calls, up to 4)** in the run view starts a separate editorial
job. It does not invoke the verifier or change its prompts, schemas, decisions,
original reports, or checkpoints. Existing web/Markdown views and offline downloads
remain available. Verification must have stopped (completed or incomplete).
The writer uses the GUI's verification model and Codex account settings.

```bash
uv run osverify report --run-dir runs/problem-001-v1 \
  --output-dir report-drafts/problem-001-v1 \
  --model YOUR_CODEX_MODEL_ID --codex-bin /path/to/codex
```

The packaged `write-referee-report` skill reads only released feedback and the
collaborator's template. It drafts the overview and issue headings/types, and places
feedback references into Concern / Check / Suggestion. Code requires every released
feedback item exactly once under its original issue. Mathematical-paper PDFs retain
exact source feedback internally and allow only recorded math-typography and
verified printed-location substitutions in the reader-facing text.
A fresh `check-referee-report` call checks editorial fidelity, not mathematical
correctness. Only an approved draft becomes `report.tex`; a withheld or failed draft
stays unpublished with its status and intermediate artifacts. Neither stage searches
for new evidence or repairs. Major/minor grouping uses the separate audited paper impact classification;
solution and older reports without that metadata remain unclassified. Empty
check/suggestion slots are omitted from the mathematical-paper PDF.

The math PDF uses the classic `amsart` title **Referee Report on “<paper title>”**,
**Review summary**, a compact completion-status table, **Major issues / Minor issues**,
exact paper quotations, bold paragraph labels, and small gray **Suggestion by OSVerify**
text. Formula typography and printed paper locators pass the editorial check.
Concise reference notes explain what was not checked and why, distinguishing an
inaccessible URL from a result checked in an alternate source. Detailed scope counts,
classification rationale, assessment tags and job paths stay in the internal JSON/Markdown
and source records.

There is one writer call and one editorial-check call, each with a 180-second default
limit (`--timeout`). If the draft fails the format checks or the editorial check withholds
it, the writer gets one more attempt with those reasons and a fresh checker judges the new
draft; there are no further retries and never a verifier rerun. Use a fresh
output directory for another attempt. Source snapshots, references, skill/template
hashes, raw call traces, draft, editorial check, and status are stored separately.
GUI outputs live in its data home's `report-drafts/`; CLI output must be outside the
source verification directory. LaTeX compilation remains a separate user action.
This experimental module lives in `osverifier_reports`, outside the verifier core.

## Paper and solution review modes

The GUI paper input selects **paper** mode. Advanced question/solution input remains
**solution** mode. For CLI paper review with an explicit scope:

```bash
uv run osverify verify --review-type paper --decompose \
  --question scope.md --paper paper.tex --request-id paper-v1 \
  --model YOUR_CODEX_MODEL_ID --run-dir runs/paper-v1
```

Paper mode emits no overall `READY_FOR_HUMAN`, `REVISE`, or `REJECT` label.
Its public JSON (`schema_version: paper-1.0`), Markdown, web view, and PDF show
review status, selected-scope coverage, and findings. A completed review does not
certify correctness. There is no default rejection stop; CLI `--early-stop` with
`--decompose` explicitly opts into stopping after a confirmed blocking finding.
Solution mode remains the default CLI mode with the original verdicts and stopping
policy. `--review-kind` remains accepted as a compatibility alias for `--review-type`. Existing runs are not migrated; use a new directory when switching modes.

After completed paper verification, a separate role reads released feedback plus the
full paper and selected scope to classify each issue by whether the released feedback
already contains a complete fix. Another role audits that classification and its
rationale. **Minor** means the released feedback supplies a concrete, complete fix,
regardless of its feedback-kind label, preserving the paper's statement. The independent
severity auditor checks the content: “change 1957 to 1952” can be a complete fix even
when tagged as a repair request. **Major** means a confirmed defect with no released
complete fix, including requests that leave an obligation unresolved and partial or
claim-changing suggestions. A suggestion label alone does not establish a complete
repair. The importance of the affected
theorem is not the criterion. Clarification-only issues, undecidable completeness,
and withheld classifications remain **unclassified**.
These calls do not re-verify findings, add issues, or rewrite released feedback.
With findings, classification adds up to two calls before the two report-writing calls.
`--no-report` skips PDF writing, not impact classification. Incomplete reviews do not
start classification; classification failure preserves findings and stops subsequent
PDF calls. Resume with the same command to retry; successful classifications are cached.

All report formats group **Major, Minor, Unclassified**, preserving original order
within each group without scores or rankings. Audited impact rationales are included.
The public `report.json` and `report.md` stay at the run root; original verification
artifacts, internal decisions, diagnostics, and checkpoints are under `.verification/`.
Classification inputs, attempts, audits, and provenance are under `paper-classification/`.
Unchecked external references appear in public and reader outputs only as title, URL
and a fixed notice; the model-written dependency notes stay internal in
`paper-diagnostics/reference-limitations.json` (see the output contract).
The original math-focused target extraction and issue-verification policies remain;
this does not yet add empirical AI-paper review.

## Python integration

```python
from osverifier import ReviewRequest
from osverifier.backend import CodexBackend
from osverifier.pipeline import RunConfig, Verifier

verifier = Verifier(
    CodexBackend(model="YOUR_CODEX_MODEL_ID", codex_bin="/absolute/path/to/codex"),
    RunConfig(workers=16, max_issues=None, max_attempts=2),
)
report = verifier.verify(
    ReviewRequest(request_id="problem-001-v1", question=question, candidate_solution=solution),
    run_dir="runs/problem-001-v1",
)
# Three decisions; None means the review has not produced a decision yet.
decision = report.decision
# This is the released channel, already checked by the feedback auditor.
feedback = [item.model_dump() for item in report.feedback]
```

Pass `report.feedback` and report status to the external generator. Do not forward
internal job artifacts as feedback: they deliberately retain unverified hypotheses.
The external framework owns revisions, subsequent submissions, and final acceptance.

## Artifacts, limits, and resume

Every run writes `manifest.json`, original inputs, `report.json`,
`report.md`, and internal `diagnostics.json`. Every stage saves its payload, attempt directories, raw responses, local
validation failures, and a hash-bound validated result. Codex attempts additionally
retain argv, prompts, schemas, event streams, stderr, session IDs and reported usage.
The report is refreshed after each finished issue; it records unfinished work separately.

Re-run the same command to reuse validated jobs. Request, supplied proposals, package
code/skills/policy, backend identity, and run configuration must match. Changed inputs
or settings require a new run directory. Failed or interrupted jobs get up to
`max_attempts` **new attempts per invocation**, each in a fresh session. Completed
stages are not repeated. This is artifact-level resume; unfinished Codex conversations
are not resumed, and their partial reasoning is not treated as a completed result.

Default proposer output has **no issue-count cap** (`max_issues=None`); it should list
all distinct issues it finds without a quota. `--max-issues N` adds an explicit cap
for each proposer invocation, including each segment when decomposition is enabled. Count caps,
context limits, or unavailable evidence that leave planned search unfinished must
be reported as incomplete; removing the count cap does not create unlimited context.

Default concurrency is **16 verifier calls** (`--workers 16`). In the decomposed
workflow, one global pool includes root/segment proposers, decomposition, duplicate
grouping and its audit, issue validators, and feedback auditors. Queued feedback
audits take priority over issue validation, followed by deduplication and then
proposers/decomposition. Priority affects the next free slot; running calls are not
preempted. Each issue is validated once, then its feedback is independently audited;
these are not multiple votes. Root-only review still runs validation/audit chains,
with the same configurable concurrency limit. Existing saved GUI settings and
explicit CLI overrides retain their chosen worker count.

Deduplication admits one proposal batch at a time, including issues whose validation
is queued or in flight. Other stages continue during those calls. Issue IDs are
scope-based, and admission order is persisted so resumed jobs reuse the same frozen
inputs even when concurrent proposers finish in a different order. Old runs are
preserved, but code/config fingerprint changes require a new run directory.

Each stage has up to 2 attempts per invocation and a 900-second per-attempt timeout
by default. In the legacy root-only workflow, N issues need `1 + 2N` calls, or at most
`max_attempts × (1 + 2N)` with retries (less with cache hits or supplied proposals).
The decomposed workflow additionally calls the decomposer and segment proposers,
plus duplicate grouping when multiple candidates exist and a fresh duplicate auditor
when semantic matches are proposed. Early stops cancel or skip remaining calls.
These controls do not impose a hard token cap inside a Codex turn.

Explicit Codex quota exhaustion bypasses the ordinary retry budget. The runtime
watches top-level provider error events, stops active calls in the affected backend,
and starts no further Codex call in that invocation; exact, verified cached results
already on disk still replay into the report. The report stays
`incomplete`, with `reason: quota_exhausted` in `interruption.json`; quota failure
never becomes mathematical feedback. Ordinary 429 throttling, network errors, and
capacity errors retain bounded retries. Text inside model/tool output is not a quota
signal. No reset-time waiting or automatic restart is performed.

After quota recovers, repeat the same command and run directory. Completed job caches
remain reusable. If calling the Python API directly, construct a fresh `CodexBackend`
for the resumed invocation because the exhausted backend deliberately remains halted.
The top-level interruption marker is cleared on the next invocation; per-attempt
records remain for provenance. Code/version changes still require a fresh run directory:
resume an existing frozen experiment using the version that created it.

The process exit code reports execution, not mathematical validity: `0` for a completed
run (including REJECT), `2` for an incomplete report, `1` for setup/contract failures,
and `130` for interruption. Consume `decision`, `run_status`, and approved `feedback`.

Version 0.2 uses output schema v2. The old public verdict/action/completion fields
were replaced by `decision`; detailed audit accounting moved to `diagnostics.json`.
Old runs remain unchanged and require a new run directory under the new code/config.

The FirstProof pilot scripts in `experiments/` use an external dataset. Set
`FIRSTPROOF_DATASET=/path/to/firstproof.jsonl` and optionally `CODEX_BIN=/path/to/codex`
before running them. Datasets and historical run outputs are not included in the repository.

## Deferred

Recursive/adaptive verifier decomposition, further generator adapters,
feedback rewriting loops, repeated-vote calibration, open-question case studies, and
ablations/comparisons are not included. This scaffold makes no empirical low-FPR claim.

### Paper review when quota is exhausted

If the provider explicitly reports exhausted quota during verification, severity
classification, or report writing, paper review saves an offline **partial report**.
No further model calls are made. Markdown and LaTeX include only already audited
feedback and approved classifications; unclassified issues remain unclassified.
A partial PDF is compiled locally when a LaTeX engine is available. The report marks
the interruption phase and coverage; completed verification with unfinished report
writing is distinguished from unfinished verification. A generic rate limit or
model text mentioning quota is not treated as exhausted balance.

The run view offers **Download partial PDF (quota exhausted)** and **Resume**.
After quota recovers, resume in the UI or rerun the identical CLI command and run
directory. Valid verifier checkpoints and approved classifications are reused.
Partial snapshots remain under `<run-name>-reports/<snapshot-hash>/<report-format-version>/partial/` while
resumed results produce the current report. Missing LaTeX preserves `.md` and `.tex`
and does not prevent resume. `--no-report` still skips PDF generation; the public
partial JSON and Markdown are always saved. CLI quota interruption exits with code 2.

## Partial solution progress

Use the original question and the current partial argument; no new question or
subproblem is needed. Both root-only and `--decompose` use the existing roles,
verbatim segmentation, full context, deduplication, exact-feedback audit, and caches.

```bash
uv run osverify verify --review-type solution --mode partial \
  --question question.md --solution progress.md --decompose \
  --model YOUR_CODEX_MODEL_ID --run-dir runs/partial-v1
```

- **PASS**: completed review found no outstanding issue or unresolved audit. Continue
  working; this does not certify correctness, completeness, or eventual success.
- **REVISE**: there is an audited problem to correct, without sufficient evidence
  to abandon the current route. No demonstrated repair is required or guaranteed.
- **REJECT**: an audited error plus independently upheld evidence of a fundamental
  obstruction to continuing the current route; it does not reject the original problem.

Explicit pending obligations and provisional assumptions are not errors just because
they remain unproved. Conditional reasoning must remain conditional, and unsupported
claims of having established a result can still be errors. Lack of a known repair
never establishes route rejection. Validators supply `route_obstruction`; partial
auditors independently set `route_rejection_upheld`. Rejecting the route argument
alone does not discard a sound diagnosis: it may still be released as revision feedback.

Only an audited route rejection triggers the existing optional early stop; use
`--no-early-stop` with `--decompose` to keep inspecting. Incomplete execution or
unresolved audits never produce PASS. Resume by rerunning the same inputs, mode,
settings and run directory; switching mode requires a fresh run. Full-solution
continues to use READY_FOR_HUMAN/REVISE/REJECT. Paper mode does not accept `--mode partial`.
Partial solution mode is currently selected through CLI or Python `ReviewRequest`;
the Rethlas-OSV GUI solution form retains its full-solution default. Rethlas-OSV reviews every generation turn: incomplete drafts use partial review,
complete candidates use full-solution review. Partial PASS continues generation;
only a completed full-solution READY_FOR_HUMAN review permits handoff.
The original question is retained, and review caches include the mode.

### PDF-only input with checked transcription

Prefer a verified LaTeX/Markdown source when one exists. Otherwise pass the PDF itself
as `--paper`, or transcribe it first:

```bash
uv run osverify ocr --pdf paper.pdf --run-dir runs/paper-ocr --model MODEL_ID
uv run osverify verify --review-type paper --paper paper.pdf --ocr-dir runs/paper-ocr \
  --decompose --run-dir runs/paper-review --model MODEL_ID
```

Poppler renders original pages; fresh Codex visual calls
transcribe and independently check every page. Corrections require another fresh
approval; unreadable pages block formal review. No external OCR account is required.
The approved text, PDF, images, line-to-page map, backend and call artifacts are frozen.
Before issue validation and again before feedback release, an independent source
check compares the allegation's cited spans and dependencies with original pixels.
OCR or layout defects block that issue chain and leave the review incomplete; they
are never reported as paper errors. Preserve old runs and reprepare/review in new
directories after a later mismatch. This implementation conservatively reruns the
full review rather than reusing judgments from altered text. Page approval is an
input-quality check, not a guarantee of perfect OCR or mathematical correctness.

The distributable OSVerify skill keeps its own `prepare-pdf` and `verify-pdf` commands
for this workflow. New skill runs use a checked literature-enabled runtime, copied into each
run's `.osverify-runtime/` for stable resume. The previous native and OCR skill
runtimes remain byte-for-byte intact; `osverify.py --legacy-runtime COMMAND ...`
selects them for their existing runs. Other historical runs must replay their own
original engine. Existing unpinned runs are never silently upgraded.

## External references during paper review

New source-based and bundled-skill paper reviews expose `osverify_literature` tools to the proposer,
issue validator and feedback auditor during their calls. `search_literature` finds
metadata; `resolve_reference` retrieves public HTML/PDF/LaTeX and follows scholarly
full-text links; `read_reference` provides original passages and requested PDF page
images. Tools are scoped to the invocation and do not change global Codex settings.

Reference access and mathematical dependency checks are separate. A failed URL
remains in the immutable retrieval history, but an independently checked alternate
source can resolve the same explicit dependency ID. Each role records inspected
URLs, versions and passage locations; one role cannot clear another's uncertainty.
Readable content alone never verifies a result or its application. Login and
subscription restrictions remain evidence limitations, not mathematical errors.

Sources are shared within a run, with per-URL process locks and append-only source
and page artifacts. Tool calls, receipts and implementation hashes are preserved.
Historical caches and runtime snapshots are not upgraded; resume those runs with
their original runtime and start a new run for these tools. The maintained skill
and `dist/osverify.zip` include both the new default and preserved legacy engines.

See [the literature-tool contract and replay instructions](literature-tools.md).

Mathematical-paper quota snapshots use the same reader layout with **Partial** status.
They make no additional model calls, retain already released findings, and explicitly
mark unfinished editing/quotation selection. Exact reference notes remain available
when concise editorial summaries cannot be produced. Normal PDF generation still
requires the independent editorial check; offline snapshots never imply that check ran.
