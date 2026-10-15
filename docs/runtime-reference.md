# OSVerify runtime reference

Implementation, integration and compatibility details for developers and operators.
For commands and everyday use, start with the [guide](guide.md).

- [Review execution](#review-execution)
- [Codex account and connection settings](#codex-account-and-connection-settings)
- [Artifacts and resume](#artifacts-and-resume)
- [Report generation](#report-generation)
- [Python integration](#python-integration)
- [Historical runs and skill commands](#historical-runs-and-skill-commands)
- [Development checks](#development-checks)

## Review execution

With `--decompose`, the whole-solution proposer and one verbatim segmentation pass
start together. Once segmentation finishes, segment proposers enter the same global
queue. Each finished proposal batch proceeds through audited deduplication and issue
review without waiting for other scopes. The model selects the segment count;
segments retain full source/search access and before/after summaries. There is one
decomposition layer, with no recursive expansion.

The default pool allows 16 concurrent verifier calls. Queued feedback audits take
priority over issue validation, followed by deduplication and then proposers and
decomposition. Priority affects the next free slot; running calls are not preempted.
Each issue is validated once and its feedback independently audited, rather than
decided by repeated voting. Root-only review uses the same configurable concurrency
limit for validation/audit chains.

Deduplication admits one proposal batch at a time, including issues whose validation
is queued or in flight. Other stages continue during those calls. Issue IDs are
scope-based; persisted admission order lets resumed jobs reuse their frozen inputs
even if concurrent proposers finish in a different order.

In solution mode, an audited rejection stops decomposed review by default. It cancels
active work for that sample and skips remaining work. `--no-early-stop` continues
inspection while preserving the rejection. Paper mode instead requires an explicit
`--early-stop`. Only finished planned coverage with no outstanding issue or feedback
can produce full-solution `READY_FOR_HUMAN` or partial-solution `PASS`.

The issue validator checks each allegation and prepares feedback candidates. A fresh
auditor checks the finding and each exact feedback text. Only approved texts enter
the public report, verbatim; the auditor cannot replace them with its own rewrite.
Withholding an optional suggestion does not invalidate an approved diagnosis.
The [output contract](output-contract.md) and
[mathematical policy](../src/osverifier/skills/policy.md) define the release and repair
rules, including missing core obligations and unresolved issues.

Partial review retains the original question. Validators supply `route_obstruction`;
partial auditors independently set `route_rejection_upheld`. A disputed obstruction
does not discard a supported diagnosis: it can still be released as revision feedback.
Paper mode does not accept partial solution review.

### Call accounting and limits

Verifier jobs allow up to two attempts per invocation, with a default 900-second
timeout per attempt. Root-only review with N issues needs `1 + 2N` calls before
retries: one proposer, then a validator and auditor for each issue. The upper bound
is `max_attempts × (1 + 2N)`; supplied proposals and cache hits reduce it.

Decomposed review adds segmentation, segment proposers and duplicate grouping; a
fresh duplicate auditor checks proposed semantic matches. PDF transcription,
original-page checks, paper target selection, reference rechecks, classification and
report writing can add further calls. Early stopping skips or cancels remaining work.
These controls do not impose a hard token cap inside a Codex turn.

`--max-issues` applies per proposer, including each segment. Its default is uncapped;
context limits, explicit caps or unavailable evidence can still leave search incomplete.

## Codex account and connection settings

Select authentication with `--codex-home`, then `$CODEX_HOME`, then `~/.codex`.
OSVerify does not run login, switch the desktop account or copy credentials. Select
the model with `--model`; `osverify models` lists the account's models. A successful
CLI version check alone does not establish model compatibility.

OSVerify reads the selected account's `config.toml` and forwards only:

- `model_provider` and the selected provider's complete `model_providers` table;
- `openai_base_url` and `chatgpt_base_url`;
- `cli_auth_credentials_store`, `forced_login_method` and `forced_chatgpt_workspace_id`.

A default inline `profile` / `profiles.<name>` selection is honored for those fields.
Provider definitions retain headers, environment-key references, query parameters,
protocol and retry settings. Model selection remains explicit. The original
authentication home and inherited environment remain in use; user config is not modified.

Other config fields, including instructions, MCP servers, hooks and feature settings,
are excluded. The transport uses `--ignore-user-config` with selected connection
settings as explicit `-c` overrides, followed by OSVerify's runtime settings. This
does not suppress every Codex discovery source: system/admin settings, separately
discovered skills, instructions, rules and installed integrations may still apply.
Named profile CLI selection and project-local connection configuration are unsupported.

Connection settings are frozen at backend construction; their source and fingerprint
enter resume provenance. Override values, including inline headers and tokens, are
redacted in `command.json`. They are passed directly to the child process and can be
visible to local process inspection, so prefer environment-backed credentials. Raw
output/tool traces are private run data. Credential values and auth-store contents
are not fingerprinted, allowing normal credential refresh.

Each role/attempt gets a fresh conversation, read-only sandbox, schema-constrained
output and JSONL events. The attempt workspace contains its assigned packaged role
skill, shared policy and immutable input. Defaults are `xhigh` reasoning and live web
search; explicit overrides are retained. Primary-source inspection is required for
reference checks. Fresh conversations separate context but do not guarantee
statistical independence of judgments.

## Artifacts and resume

The public result is `report.json` and `report.md` at the run root. For paper review,
core verification artifacts live under `.verification/`. For solution review they
live directly in the run directory.

| Artifact | Contents |
| --- | --- |
| `manifest.json` and saved inputs | Request and code, policy, skill, schema, backend and configuration provenance. |
| `diagnostics.json` | Internal findings, missing-content flags, withheld advice, unresolved issues and execution errors. |
| Job/attempt directories | Exact payload, schema, skill/policy, command, response, validation errors, events, stderr, session IDs, reported usage and hash-bound validated result. |
| `trajectory.jsonl` / `trajectory.md` | Decomposed-review events: nodes, attempts, cache hits, deduplication, decisions, skips, failures and cancellations. |
| `nodes/`, `candidates.json`, `aliases.json` | Decomposed scopes and verbatim excerpts, allegations and audited duplicate links. |
| `paper-inputs/` / `pdf-input/` | Frozen paper source or checked PDF transcription and its source artifacts. |
| `paper-classification/` | Classification inputs, attempts, audits and provenance. |
| `paper-diagnostics/reference-limitations.json` | Internal dependency notes; public reports expose only the permitted reference notices. |

Reports are refreshed as work finishes. Internal allegations and withheld feedback
must not be forwarded as audited findings. Reference receipts, tool journals and
implementation hashes follow the [literature-tool contract](literature-tools.md).

Resume requires matching requests, supplied proposals, code/skills/policy, backend
identity and configuration. Failed or interrupted jobs receive up to `max_attempts`
new attempts per invocation. Completed jobs replay validated caches. Unfinished Codex
conversations and partial reasoning are not resumed as completed results. A completed
workflow verifies its hashed checkpoint and does not restart skipped or cancelled work.

### Quota interruption

Explicit provider quota errors stop active calls in the affected backend and prevent
further dispatch in that invocation. Exact verified caches can still replay. Ordinary
429 throttling, network/capacity errors and model/tool prose mentioning quota do not
trigger this halt. `interruption.json` records `reason: quota_exhausted`; paper reports
also identify the interrupted phase.

After quota recovery, replay the same command. Python callers must construct a fresh
`CodexBackend`, since the exhausted instance remains halted. The interruption marker
is cleared on the next invocation; attempt records remain for provenance.

## Report generation

The offline `export` command reads released reports and makes no model calls. LaTeX
uses the `amsart` template, groups exact feedback by issue and retains audit references.
It does not infer severity or an overall mathematical opinion when absent from the
source report. Markdown export preserves the existing snapshot. Older Markdown-only
runs can export their original section structure to LaTeX.

Markdown-to-LaTeX conversion supports headings, lists, emphasis, code, tables and
`$...$`, `$$...$$`, `\(...\)`, `\[...\]` math. Links retain labels and destinations;
raw HTML is literal. Export requires neither Pandoc nor TeX; compilation can require
custom macro definitions or fonts.

Paper findings receive a separate repair-based classification and independent audit.
These calls inspect the released feedback, paper and scope without adding findings or
rewriting feedback. Successful classifications are cached. Classification failure
preserves findings and prevents subsequent automatic PDF calls. Ordinary incomplete
reviews with released findings can still be classified; quota or execution
interruption can skip classification. `--no-report` does not skip classification.

The report writer and fresh editorial checker inspect released feedback and require
every feedback item exactly once under its original issue. The check concerns
editorial fidelity. Neither role searches for new mathematical evidence or repairs.
For paper PDFs, recorded math-typography and verified printed-location substitutions
are permitted; exact source feedback remains intact. See the
[PDF output contract](output-contract.md#reader-facing-mathematical-referee-pdf).

The writer/checker use a 180-second per-call default; `report --timeout` changes it.
An explicit `verify --timeout-seconds` also applies to its report/classification calls.
A failed format check or withheld draft permits one more writer attempt and a fresh
checker, for up to four writer/checker calls. Paper quotation selection and reference
notes use the approved source records. Standalone `report` requires a fresh output
directory outside the verification run and saves approved `.tex` for separate compilation.

Automatic reporting stores attempts under
`<run-name>-reports/<report-json-sha256>/<report-format-version>/`. A completed PDF is
reused. If no TeX engine is available, approved text waits in `pending-report.tex`;
installing the engine allows compilation without repeating the writer. A draft that
actually fails compilation remains diagnostic material; a retry drafts and checks a
new report. `report.tex` in the automatic output directory is published only after
successful compilation. Compiler runs disable shell escape and restrict file access.

PDF failure is reported separately from the review result. The offline demo and
internal Rethlas review loop do not trigger automatic reporting. Rethlas-OSV owns its
GUI and download controls; see the [companion repository](https://github.com/OSVerify/Rethlas-OSV).

On paper quota exhaustion, offline partial snapshots retain audited feedback and
approved classifications, mark unfinished review/editorial work, and make no model
calls. They are stored under the report directory's `partial/` subdirectory. Missing
TeX preserves Markdown and LaTeX. Snapshots do not imply a completed editorial check,
and remain available after resume produces the current report.

## Python integration

This example uses whole-solution review. Use `DecomposingVerifier` and `WorkflowConfig`
from `osverifier.workflow` for the decomposed workflow.

```python
from pathlib import Path

from osverifier import ReviewRequest
from osverifier.backend import CodexBackend
from osverifier.pipeline import RunConfig, Verifier

verifier = Verifier(
    CodexBackend(model="gpt-6-astra"),
    RunConfig(workers=16, max_attempts=2),
)
report = verifier.verify(
    ReviewRequest(
        request_id="problem-001-v1",
        question=Path("question.md").read_text(),
        candidate_solution=Path("solution.md").read_text(),
    ),
    run_dir="runs/problem-001-v1",
)
decision = report.decision
status = report.run_status
feedback = [item.model_dump() for item in report.feedback]
```

Set `mode="partial"` on `ReviewRequest` for unfinished progress. `--request` accepts
the same JSON through the CLI. `--proposals` supplies a `ProposalBatch` and skips the
proposer only in root-only solution review.

Pass released feedback together with status to an external generator. The external
framework owns revisions, submissions and final acceptance. In Rethlas-OSV, partial
`PASS` permits continued generation; only a completed full-solution
`READY_FOR_HUMAN` review permits handoff.

## Historical runs and skill commands

Use a new run directory when changing source, mode, configuration or code. Preserve
old artifacts and replay historical source runs with the engine that created them.
The current package has root-only schema v2, decomposed schema v3 and separate paper
schemas; consult each report's `schema_version`. V1 fields `verdict`, `next_action`,
`completion` and `feedback_review_status` were replaced by `decision` and internal
diagnostics. `--review-kind` remains a compatibility alias for `--review-type`.

Current agent-skill commands match the CLI: `verify`, `ocr`, `report` and `export`,
invoked through `uv run --script /path/to/skill/scripts/osverify.py ...`. New skill
runs pin a checked copy of `scripts/review-runtime/` in `.osverify-runtime/`; later
invocations use that copy even after a skill update.

`prepare-paper`, `prepare-pdf` and `verify-pdf` are historical compatibility commands.
Runs started with them keep their original command and pinned engine. The older
native and OCR engines remain under `scripts/runtime/` and `scripts/ocr-runtime/`;
`osverify.py --legacy-runtime COMMAND ...` selects them for their existing runs.
Unpinned historical runs require their original engine and are never silently upgraded.

## Development checks

Run from the source checkout:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run osverify schemas --out schemas
```

Scripted tests and the offline demo establish software behavior; they do not measure
mathematical review quality or false-rejection rates. Recursive/adaptive decomposition,
feedback rewriting loops and repeated-vote calibration are not implemented here.

The FirstProof pilot scripts in `experiments/` require an external dataset. Set
`FIRSTPROOF_DATASET=/path/to/firstproof.jsonl` and optionally `CODEX_BIN=/path/to/codex`.
Datasets and historical run outputs are not included. See [AGENTS.md](../AGENTS.md)
for contributor contracts.
