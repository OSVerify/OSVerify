---
name: validate-solution-issue
description: Independently verify one proposed AI/ML correctness finding and prepare exact feedback for audit.
---

Read [the AI/ML paper policy](../policy.md) and input.json. Reconstruct the actual
claim, modality, scope and supporting evidence from the full original paper. Treat
the allegation and suggestions as hypotheses. Actively look for a refutation,
qualifier, relevant appendix, legitimate convention or narrower actual use. Check
numbers, statistical reasoning and relied-on external sources yourself.

Decide confirmed/not_real/unresolved, kind, impact and completion_effect separately.
For an evidence gap identify the necessary obligation, full-context check and exact
failure of the supplied evidence. Distinguish unsupported inference from false
conclusion, unavailable material from absent support, and optional improvement from
required correction. Do not confirm novelty or significance complaints.

The internal rejection_supported flag only controls optional early stopping; apply
the policy's blocking-impact and supported-repair rules. No paper acceptance score
or recommendation is requested. A suggested new experiment is not a successful
repair; withdrawing/narrowing a claim does not establish the original claim.

Return IssueReview for exactly the input issue ID. A confirmed issue requires source
spans and a diagnosis. not_real has no feedback. unresolved permits only calibrated
clarification requests. Prepare separate diagnosis, repair_request and optional
repair_suggestion candidates. State precisely what each check establishes and
what remains uncertain. Advice that changes the claim must explicitly say so.
