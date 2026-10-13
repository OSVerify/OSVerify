---
name: group-duplicate-issues
description: Group repeated atomic allegations in an AI/ML correctness review.
---

Read [the AI/ML paper policy](../policy.md) and input.json. Propose duplicate groups
only when the candidates allege the SAME defect against the SAME target under the
same conditions. Shared table, equation, experiment or claim alone is not enough.
Never merge a calculation error with an evidence gap, or different possible
confounds, merely because they affect the same conclusion. Preserve distinct
atomic allegations and all original IDs. Do not invent new allegations, rewrite
feedback, decide realness or assess novelty. Uncertain matches stay separate.
The incoming candidate must add no material evidence, counterexample, downstream
consequence or repair suggestion that could change adjudication. Each proposed
link points from a new candidate to an earlier candidate in supplied order.
Return DuplicatePlan matching the supplied schema; use input IDs only. An empty
match list is valid; Python retains originals and independently audits every link.
