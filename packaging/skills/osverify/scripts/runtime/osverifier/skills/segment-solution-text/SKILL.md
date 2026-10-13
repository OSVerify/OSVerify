---
name: segment-solution-text
description: Divide a supplied mathematical solution into verbatim contiguous segments and add brief descriptive summaries of relevant preceding and subsequent context, in one decomposition pass.
---

For `request.mode=partial`, the shared policy's Partial-progress mode section
overrides full-solution completeness, rejection, repair and handoff rules below.


Read the shared [policy](../policy.md). Read `input.json`, `question.md`, and `numbered-solution.md`; `candidate-solution.md`
contains the exact unnumbered original. Choose natural boundaries in the submitted
solution. You may read or search any part of these originals, use web search when
useful, and choose your own reading order.

Return a single ordered list of segments covering the full supplied scope. Select
the number of segments yourself, with no fixed branching factor or count quota.
If useful division is unwarranted, retain the scope as one segment. Favor meaningful
argument units over balanced lengths or sentence-by-sentence splitting. This call
performs one pass only; do not recursively subdivide the resulting segments.

For each segment, provide its one-based inclusive original start/end lines and two
brief context summaries:
- `context_before`: the relevant definitions, assumptions, constructions, and results
  that the preceding text claims to have established. Describe the submitted argument
  without certifying those results.
- `context_after`: what the subsequent text attempts to prove and how it uses this
  segment, where the original makes that connection clear. Describe the actual later
  text rather than inventing a proof plan. Later results are not available premises
  for the current segment.

Keep summaries focused on context useful for this segment rather than recounting
the whole prefix or suffix. Empty summaries are appropriate when there is no relevant
preceding or subsequent content. Preserve mathematical qualifications and uncertainty.

The orchestrator will extract segment text verbatim from your boundaries. Do not
rewrite the solution, generate new proof obligations, judge correctness, or propose
issues or repairs. A segment need not be a self-contained proof: its future proposer
will retain access to the full question, original solution, and web search. The
whole-solution proposer also remains in the intended workflow.

Return only JSON matching the supplied schema.
