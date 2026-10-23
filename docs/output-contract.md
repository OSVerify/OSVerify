# Output contracts: decomposed workflow v3 and legacy root-only v2

Use **one decision**, approved feedback, and execution status to integrate this
verifier. The schemas are generated from `src/osverifier/models.py` with
`uv run osverify schemas --out schemas`.

The `--decompose` workflow uses `WorkflowReport` from `workflow_models.py`, with
`schema_version=3.0`. It keeps the decisions and exact-feedback fields below and adds
`termination_reason`, `coverage_complete`, and `trajectory_artifact`. The legacy
root-only `ReviewReport` remains version 2.0.

Root review and one decomposition pass start together. Segment proposers join the
global queue as soon as decomposition finishes. Each finished proposal batch is
deduplicated and validated without waiting for other proposers. The default limit
is 16 active verifier calls, with feedback audits ahead of issue validation, then
deduplication and proposers. Source spans and node display order stay in source order;
issue IDs are scope-based and dedup admission order is frozen for resume. Any rejection
must pass the same issue and exact-diagnosis audit gate before it can stop the run.
The stop cancels active work for that sample and skips queued issues/nodes. An
intentional stop is a completed workflow with partial coverage, never a claim that
all mathematical content was reviewed. Its reason is `audited_rejection`; fully
finished traversal uses `all_nodes_reviewed`; unfinished execution/audits use
`execution_incomplete`. When every planned job finished but unresolved or audit-blocked
findings, or a search the proposer could not finish (for example an unlocatable cited
source), leave the run `incomplete`, the reason is `review_inconclusive`: resume replays
the same cached results and cannot change it. Human readiness additionally requires
complete planned coverage.

Each segment retains the full original request, numbered source, before/after context
summaries, and verbatim focused excerpt. Only root has responsibility to check that
the entire question is solved. Summaries cannot create premises or proof obligations.

Duplicate grouping only aliases an incoming issue after a fresh duplicate auditor
confirms equivalence with an earlier allegation and absence of material new evidence
or repair advice. Originals and both grouping decisions remain in the trajectory.
Unconfirmed links retain separate issues; grouping execution failure falls back to
reviewing every incoming candidate. Deduplication never rewrites released feedback.

`diagnostics.json` version 3 adds node states, issue origins, aliases, stop basis,
skipped issue IDs, and coverage. Each node's internal `review.json` is scope-only;
only the top-level report is a workflow handoff. `trajectory.jsonl` is append-only,
including cache reuse on resume. The readable trajectory is internal review material,
not audited feedback. CLI logs contain available outputs, not guaranteed hidden reasoning.

## Public decisions

The table below describes full-solution review. Partial progress uses `PASS` instead
of `READY_FOR_HUMAN`; paper review has no overall solution verdict. See the
[guide](guide.md#solution-decisions) for the modes side by side.

| `decision` | Meaning and next step |
| --- | --- |
| `READY_FOR_HUMAN` | Automated review finished with no outstanding issue or revision feedback. Send the candidate for final human confirmation. |
| `REVISE` | Return approved feedback for supported local repairs, local omissions, minor issues, and justified clarification requests. |
| `REJECT` | A confirmed blocking error or demonstrated missing core obligation warrants rejection under the repair policy, with basis and diagnosis upheld by the auditor. |

Precedence is `REJECT`, then `REVISE`, then `READY_FOR_HUMAN`. Every revision or
rejection includes audited feedback.
There is no separate public category for optional minor feedback.

A supported local repair routes a confirmed error to `REVISE`, even when the error
blocks the unrevised proof. The validator must justify a concrete correction, preserve
the original question, and explain how affected downstream uses remain valid within
the existing proof route. It records this in `rationale` and `evidence`, keeps
`assessment=confirmed` and the accurate issue kind, and sets `rejection_supported=false`.
The auditor independently checks that decision and all outward feedback. No additional
state or schema field is introduced. Speculative repairs do not clear established
blocking errors. Policy 2.3 permits rejection for demonstrated core incompleteness in
full-solution mode: identify the indispensable obligation and where the submitted
argument fails to discharge it, after checking the full context and legitimate
implicit justification. The schema requires confirmed, blocking, missing required
content for an omission-based rejection; the validator and auditor must establish
its mathematics. Missing routine detail or failure to find a repair is insufficient.
Supported local completions use revision. Uncertain allegations cannot reject, and
already justified steps must not generate unnecessary revision feedback. No output
fields changed; package 0.3.1 changes validation and policy semantics, so use new runs.

Human readiness is the review's recommendation to obtain final human confirmation,
not a formal correctness certificate. It requires the proposer to finish its planned
search, all issue/audit jobs to finish, no unresolved issue needing further review,
and no released feedback requiring revision. Disproved issues can be cleared when
the feedback auditor upholds that assessment.

## Unfinished work

`run_status` is `running`, `completed`, or `incomplete`. Timeouts, invalid model
outputs, unfinished searches, disputed diagnoses, and unresolved issues without
audited clarification do not authorize human readiness.

If such a run has no usable decision yet, `decision=null`. This is workflow execution
state, not a fourth mathematical verdict. Already supported `REVISE` or `REJECT`
decisions and partial audited feedback can coexist with an incomplete run; consumers
must not treat them as an exhaustive review. The CLI returns exit code 2 when unfinished.

An unresolved issue with an approved, uncertainty-aware clarification request uses
`REVISE`. An unresolved issue without usable audited feedback leaves the review
incomplete. Feedback rewriting and automatic adjudication of audit disagreement
are not implemented; those cases retain their internal artifacts for follow-up.

## Public JSON

The principal fields are:

```json
{
  "schema_version": "2.0",
  "request_id": "problem-001-v2",
  "mode": "full_solution",
  "decision": "READY_FOR_HUMAN",
  "run_status": "completed",
  "backend_kind": "codex",
  "feedback": [],
  "diagnostics_artifact": "diagnostics.json"
}
```

Each released feedback item contains its issue/candidate IDs, type, **exact approved
text**, original source spans, and audit artifact path. Internal mathematical
rationales, candidate allegations, and withheld suggestions are not release channels.
The Markdown report uses the same texts, with no model-generated summary after audit.

## Internal review contracts

The public interface is simple while the internal gates remain explicit:

1. `ProposalBatch`: whole-solution scope, search status, limitations, and distinct
   candidate issues. Each has a target, source spans, allegation, evidence, and draft
   feedback. The default issue count is uncapped. Proposal alone never rejects.
2. `IssueReview`: issue realness (`confirmed`, `not_real`, `unresolved`), kind, impact,
   rejection support, any missing-content effect, source spans, evidence, and feedback
   candidates. The validator may rewrite the proposer's feedback before submitting it
   to the next gate. A confirmed issue needs at least one diagnosis; unresolved issues
   permit only cautious clarification requests; disproved issues have no feedback.
3. `FeedbackAudit`: independently uphold, dispute, or remain uncertain about the issue
   review, and approve/withhold every exact feedback text. There is **no replacement-text
   field**. Approval requires source grounding, relevance to the reviewed issue,
   preservation of the question, mathematical support, and calibrated uncertainty.
4. Python release: every feedback ID must be audited exactly once. A disputed review,
   uncertain review, or withheld diagnosis blocks that finding's entire release and
   rejection basis. Withholding an optional remedy does not block an independently
   approved diagnosis or repair request. Approved text is copied verbatim.

Each issue's validator and feedback auditor run sequentially in fresh Codex sessions.
Different issue chains run concurrently, defaulting to at most 16 active Codex calls.
This is concurrency, not repeated voting. All three roles default to `xhigh` effort
and live web search; explicit runtime overrides are preserved.

## Supplied reference files

`Verifier.verify(..., references=DIR)` and `DecomposingVerifier.verify(..., references=DIR)`
(CLI `--references`, solution review only) freeze a directory of user-supplied files with
the review. Each regular file is copied to `supplied-references/files/<name>`, and
`supplied-references/catalogue.json` lists its `name` (relative path), `sha256`, `bytes`
and `format` (`pdf`, `image`, `text` or `unsupported`, decided by content). Symbolic
links and empty directories are refused; macOS Finder metadata (`.DS_Store`, `._*`) is
ignored. `manifest.json` provenance gains `supplied_references`: the file hashes and the
code hashes of the reader modules, which are checked again before each reading call.
Runs without references keep their previous provenance and model inputs.

Only proposer, issue validator and feedback auditor inputs contain
`supplied_references` (the catalogue, without paths); segmentation and deduplication
inputs are unchanged. Those three roles get an invocation-scoped
`osverify_supplied_references` MCP server, the `--supplied` mode of
`osverifier_reports.reference_tools`, with `list_supplied_references` and
`read_supplied_reference`. It accepts only catalogue names, re-checks a file's hash on
every read, and has no network or other filesystem access. Derived PDF text and page
images go to that attempt's `supplied-reference-reads/`, and its tool journal to
`supplied-reference-calls.jsonl`; both are hashed into the job's cached result.

Decisions and output fields are unchanged. Under policy 2.5 a supplied file is a source
to read and compare under the citation procedure, not proof: reviewers check that it is
the cited work, then the statement and the submission's application; an unreadable or
non-matching file leaves the dependency uncertain. Resume requires identical directory
contents. The frozen copy and its catalogue are verified before any model call (and
before a quota interruption record is cleared), and are part of the completion checkpoint. To add references to an existing review, start a new run
directory (Rethlas-OSV's `--seed-from` does this for an experiment).

## Diagnostics and migration

`diagnostics.json` stores issue counts and statuses, missing-content flags, withheld
feedback counts, unresolved issue IDs, search status, execution errors, scope limits,
and the provenance manifest path. It supports inspection and recovery; it is not
feedback to send automatically to the generator.

V1 public fields `verdict`, `next_action`, `completion`, and `feedback_review_status`
are removed. Missing-content and uncertainty judgments remain internal. Historical
run artifacts are unchanged; new code/config/schema fingerprints require a new run
directory. The supplied demo is scripted, explicitly labeled, and is not a real
mathematical evaluation.


## Precise paper passages in referee exports

The editorial referee export resolves printed references from the frozen paper,
then selects the actual disputed sentence/formula separately from supporting
context. Each issue with available source text requires a verbatim source quote,
a readable transcription, a narrow source span, and an explanation. Definitions
and downstream applications have separate roles and relevance descriptions.
The independent editorial check verifies role and transcription fidelity; quote
and span mismatches block export before that check. Missing source is reported
explicitly, never filled with invented quotations. Original released feedback,
severity and frozen review files are not rewritten.

`paper-locations/locations.json` stores source candidates and per-line printed
references, not error assertions. `issue-passages.json` stores the approved role
selections, source quotations and source hash for reuse by public issue pages.
`--paper-assets` supplies explicit local LaTeX assets; `--paper-pdf` permits
unambiguous original-PDF heading fallback when compilation is unavailable.

Referee PDF export uses the TeX-distributed FreeSerif and DejaVu Sans Mono fonts
(MacTeX/TeX Live font packages) to preserve Unicode mathematical symbols. Missing
glyphs stop compilation rather than silently dropping symbols. Explicit LaTeX
math is preserved, and bare identifiers such as `p_*ω` are not treated as Markdown
emphasis. Long source paths and URLs can wrap without changing visible text.

Automatic exports use a format-version subdirectory under the source-report hash.
A new-format export cannot reuse an old-format PDF, and historical PDFs remain
untouched. Repeated exports in the same format still reuse the approved result.


Referee exports display repair requests and suggestions under **Proposed repair
by OSVerify**, preserving their exact audited text and internal feedback kinds.

## Reader-facing mathematical referee PDF

The default mathematical-paper PDF is an editorial view of the released report,
not an additional verifier decision. It uses `Referee Report on` the paper title,
Review summary, a single completion-status table, Major/Minor/Unclassified issues,
source quotations, bold paragraph labels and small gray suggestions. Internal
scope counts, assessment tags, severity rationales and job paths stay in the source
records. Concise reference notes preserve dependency, access and version limitations;
they are drafted from the internal reference records and pass the editorial check.
Outside that audited summary, every reader-facing output (public paper `report.json`
and `report.md`, the AI reader's HTML/Markdown/PDF and `source-report.json`, and
offline quota snapshots) names an unchecked reference only by title and URL with one
fixed sentence: "A cited dependency could not be checked; this is a limit of the
review, not an established defect." Model-written dependency and note text, which
can contain internal line numbers and unaudited judgments, stays in the run's
internal `paper-diagnostics/reference-limitations.json` and job observations.
Each math/locator presentation edit is anchored to an occurrence in exact approved
feedback; validation rejects symbol changes, unanchored locators and overlapping
edits. An independent editorial checker verifies meaning and locator precision.
Source feedback and classifications are unchanged. Offline quota snapshots use
this layout with explicit Partial/unfinished-editorial labels and no model calls.
Report caches include the format version so old PDFs cannot mask a newer renderer.
