# OSVerify

The core reviews a supplied question and candidate solution; it does not generate or
edit solutions. Generation (Rethlas) and the GUI/desktop app live in the separate
Rethlas-OSV repository, which drives this core through its public interfaces. Implement only full-solution verification mode.
The decomposed workflow reviews the root first, then performs one verbatim segmentation
pass with before/after summaries. It reviews segments in original order, preserves full
source/tool access, audits semantic deduplication, and stops on an audited rejection.
Keep the legacy root-only entry point available and preserve all historical runs.

Use `uv sync`, `uv run pytest`, and `uv run ruff check .`.

Preserve these contracts:
- Expose only `READY_FOR_HUMAN`, `REVISE`, or `REJECT` as workflow decisions.
  Human readiness requires finished review with no unresolved issue or feedback;
  it is not a mathematical proof certificate. An unfinished review may have no decision.
- Full-solution rejection may cover a demonstrated missing core obligation: identify
  the required result and why the full submitted argument fails to supply it. Mere
  missing detail, implicit standard steps, or inability to verify/find a repair are
  insufficient. Do not import the older competition-grading policy.
- A confirmed error with a supported local repair uses `REVISE`: justify the correction,
  preserve the question, and reconnect affected downstream uses within the existing
  proof route. Keep the original issue confirmed; audit the decision as well as the text.
  Speculation does not establish repairability. A real local omission may still request
  revision without certifying a repair; unresolved core-omission allegations cannot reject.
- An issue, its severity, and its proposed feedback are separate review objects.
- Publish only exact feedback texts approved by the feedback auditor. That auditor
  may withhold text and veto a finding; it cannot introduce an unreviewed rewrite.
- Keep detailed audit states in `diagnostics.json`. Execution/audit failure must
  never become a clean human handoff. Minor issues use `REVISE`; already justified
  steps and optional exposition must not become manufactured revision requirements.
- Defaults prioritize thorough review: no issue count cap, 16 concurrent verifier calls
  (`--workers 16`), reasoning effort `xhigh`, and live web search. Preserve explicit
  user overrides.
- Preserve request, policy, skill, schema, code, and backend provenance on resume.
- Record every node/job/attempt, duplicate link, approved rejection, skipped task, and
  cancellation in the trajectory. An intentional rejection stop completes the workflow
  with partial coverage; it does not mean all nodes were reviewed. Only fully finished
  planned coverage can produce READY_FOR_HUMAN.
- Never treat a subprocess exit code or a schema-valid response as evidence of
  mathematical correctness. Tests with scripted backends establish plumbing only.
- Explicit provider quota exhaustion stops this invocation without ordinary retries
  or further backend calls; exact cached results may still replay. Preserve completed
  caches, record an incomplete review, and resume manually with the same frozen inputs.
  Do not classify model/tool prose or generic 429 throttling as exhausted quota, or
  forward quota errors as feedback.

Transport must inherit only selected user API/provider/auth configuration, never the
whole user config. Apply the same transport snapshot to verifier and generator; redact
connection overrides in persisted commands and preserve their fingerprint on resume.

Role skills are maintained under `src/osverifier/skills/`, bundled in the Python
package and linked from `.agents/skills/`. Runtime calls copy only their own skill
and the shared policy into a separate call workspace. Do not install global skills
or change the desktop Codex account as part of this project.

Rethlas-OSV (github.com/OSVerify/Rethlas-OSV) depends on this package. Keep the
interfaces it uses stable: `CodexBackend` (including `start_process`,
`release_process`, `raise_if_stopped`, `record_event_metadata`, `halt_for_quota`,
`cancel`), `ConnectionConfig` redaction, the quota helpers, `Verifier` /
`DecomposingVerifier`, the report models, `osverifier_reports.paper_prepare`, and the
CLI flags. Treat a breaking change there as a minor-version bump.
Helper processes (verifier, paper extraction, PDF review) must be spawned through
`osverifier.frozen.python_module_command`, never a literal `python -m`, so the compiled
desktop bundle can dispatch them with `--entry`.
