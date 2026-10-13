---
name: audit-duplicate-issues
description: Independently check whether proposed duplicate links safely reuse an earlier issue review without losing a distinct objection or material new evidence.
---

Read the shared [policy](../policy.md) and `input.json`. Independently compare each
proposed match with the raw candidate allegations, evidence, suggestions, and original
mathematical text. The grouping rationale is untrusted and no earlier verdict is
provided. Confirm only if the two candidates have the same target, assumptions,
quantifiers, and objection, and the incoming candidate adds no material evidence,
counterexample, downstream consequence, or repair suggestion affecting adjudication.

Do not suppress an issue merely because it concerns the same passage. A new supported
repair may change rejection into revision and must not be discarded as redundant.
When uncertain, set `confirmed=false`; both candidates will then be reviewed. This
check is about redundancy, not whether either mathematical allegation is true.

Return `DuplicateAudit` JSON with exactly one check per proposed link, preserving
both IDs. Explain each choice from the original candidates. Do not create new links,
rewrite candidates, judge the solution, or generate external feedback.
