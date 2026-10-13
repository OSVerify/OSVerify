# Solution review

Store `question.md` (the full original problem) and `solution.md` (the supplied
candidate) in a new run's `inputs/` directory. For material supplied in chat, copy it
faithfully. For existing files, preserve the originals and record their source paths.

Run, substituting absolute paths and the chosen model:

```bash
uv run --script /absolute/skill/scripts/osverify.py verify \
  --review-type solution --mode full_solution --decompose \
  --question /absolute/run/inputs/question.md \
  --solution /absolute/run/inputs/solution.md \
  --request-id solution-v1 --run-dir /absolute/run
```

Use `--mode partial` for partial progress; all other input and review mechanics stay
the same. `--decompose` starts root review and one verbatim segmentation pass in
parallel, then pipelines segment proposals and issue reviews through a shared pool
(default `--workers 16`). Feedback audits and validation take priority over queued
proposals, with full context retained. It is not recursive decomposition. Omit it only for an
explicit root-only review. The default stops on audited rejection;
`--no-early-stop` continues remaining scopes if the user requests full coverage.

## Meaning of decisions

| Mode | Clean completed review | Revision | Rejection |
| --- | --- | --- | --- |
| full_solution | READY_FOR_HUMAN: ready for human confirmation | REVISE: approved local correction or clarification | REJECT: audited blocking error or demonstrated missing core obligation under the repair policy |
| partial | PASS: continue developing this route | REVISE: correct supplied progress; repairability need not be established | REJECT: audited concrete fundamental obstruction to the current route |

In partial mode, clearly stated pending obligations and provisional assumptions are
not errors simply because unfinished. Check conditional reasoning without certifying
its premises. Mere incompleteness, a false intermediate lemma alone, or inability to
find a repair cannot justify route rejection.

`run_status=incomplete` may still contain usable audited feedback. A null decision
means no decision yet. Neither incomplete review nor unresolved audits support a
clean PASS/handoff. Read coverage and termination reason even when completed.

The engine normally creates reader/PDF reports separately. `--no-report` skips
additional report writing calls, retaining the core reports. Existing results can
be exported offline:

```bash
uv run --script /absolute/skill/scripts/osverify.py export \
  --run-dir /absolute/run --format markdown --output /absolute/run/approved.md
```
