---
name: group-duplicate-issues
description: Identify genuinely redundant issue candidates without rewriting allegations or discarding new evidence or repair suggestions.
---

Read the shared [policy](../policy.md) and `input.json`. You receive the immutable
question and full solution, earlier canonical candidates, and a new candidate batch.
Only the original allegations and suggestions are supplied, not earlier verdicts.

Propose a duplicate link only when both candidates challenge the same mathematical
claim or inference, under the same assumptions and quantifiers, for the same reason.
The incoming candidate must add no material evidence, counterexample, downstream
impact, or possible repair that could change adjudication. Mere overlap in location,
topic, or suspected root cause does not establish equivalence. A broader allegation
and a narrower one are not automatically interchangeable. Read the raw context when
equivalence depends on it. Segment context summaries are not authoritative premises.

Each link must point from a new candidate to an earlier candidate in the supplied
order. Omit uncertain matches and keep materially different candidates separate.
Do not rewrite any allegation or feedback. An empty match list is valid. Python
will retain the originals and independently audit every proposed semantic link.

Return `DuplicatePlan` JSON with candidate IDs exactly as supplied and concise
source-grounded reasons. This role makes no mathematical correctness verdict.
