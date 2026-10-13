---
name: audit-duplicate-issues
description: Independently audit proposed duplicate groups without conflating distinct AI/ML defects.
---

Read [the AI/ML paper policy](../policy.md) and input.json. Independently check each
proposed group against the original candidates and paper. Approve only the SAME
atomic allegation against the SAME target/defect. A shared table, experiment or
conclusion is insufficient. Do not collapse a gap, false conclusion and invalid
attribution into a bundle. Do not decide correctness or novelty. Reject uncertain
groupings so they remain independently reviewable. The incoming candidate must add
no material evidence, counterexample, downstream consequence or supported repair
that could change adjudication. Return DuplicateAudit with exactly one check per
proposed link, preserving both IDs and giving an internal justification. Set
confirmed=false when uncertain; do not introduce links or external feedback.
