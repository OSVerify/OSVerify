---
name: audit-verifier-feedback
description: Audit an AI/ML correctness finding and each exact feedback text before release.
---

Read [the AI/ML paper policy](../policy.md) and input.json. Independently check the
allegation and issue review against the original claim, evidence, settings and full
context. Verify relied-on computations and citations yourself. Uphold, dispute or
remain uncertain; do not defer to the validator. Check kind, impact, completion and
the optional early-stop basis separately. Check proposed repairs and affected uses.

Specifically catch unsupported claims of leakage, implementation bugs, significance,
causation or failed generalization. Do not turn evidence gaps into false conclusions.
Reject novelty/significance judgments and unnecessary experiment demands. Confirmed
gaps need an explicit claim, necessary evidence and full-context check. Missing
access is not proof of absent support. Unresolved issues allow cautious clarification
only. A speculative experiment or a narrower claim cannot clear the original defect.

The complete release-candidate list is `issue_review.feedback_candidates` in
input.json. Audit only these exact candidates, never earlier proposer drafts or
other text in the allegation. If this list is empty (including a `not_real`
review), return `decisions: []`; still independently audit the issue review and
its assessment. Do not invent decisions to explain why no feedback is warranted.

Audit every candidate in that list verbatim using the five schema checks:
grounded_in_source; addresses_validated_issue; preserves_problem (faithful to the
original claim; explicitly labeled narrowing is only an option, not its proof);
mathematically_supported (including empirical/statistical support); and
uncertainty_calibrated. Approve only if all hold. A mixed supported/unsupported text
must be withheld as a unit. Disputed/uncertain reviews release nothing. A withheld
diagnosis blocks the finding's whole bundle; optional advice may be withheld alone.

Return FeedbackAudit for exactly the input issue ID with one decision for every
`issue_review.feedback_candidates` ID and no other IDs. Do not rewrite, add
findings, or release your internal rationale.
