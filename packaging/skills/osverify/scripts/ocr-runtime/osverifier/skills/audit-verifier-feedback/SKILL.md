---
name: audit-verifier-feedback
description: Independently audit a verifier finding and every proposed feedback text before release, withholding unsupported diagnoses or repair advice without rewriting them.
---

For `request.mode=partial`, the shared policy's Partial-progress mode section
overrides full-solution completeness, rejection, repair and handoff rules below.


Read [the shared review policy](../policy.md) and the call workspace's `input.json`.
You receive the original question and candidate solution, the original allegation,
and an issue review containing feedback candidates. The review is not authoritative.

Check whether the issue review is upheld, disputed, or uncertain from the original
mathematics. In particular, check whether missing proof has been confused with a false
statement, whether core incompleteness is actually established, and whether the impact
matches the challenged statement's role. Do not defer to the previous validator's label.

Check `rejection_supported` independently of whether the diagnosis is true. Apply the
shared policy's local-repair criteria: verify the correction, preservation of the
question, and the bridge to affected downstream uses. A literal error can be real and
blocking in the submitted text while a supported local repair calls for revision.
Conversely, a speculative fix must not be endorsed as established. If rejection is
wrongly supported despite a supported local repair, dispute the review even when its
diagnosis is accurate. If the review relies on a repair whose validity cannot be
established, mark the review uncertain or disputed as the evidence warrants. For an
omission-based rejection, independently check the specific required obligation, the
submitted support and full-context check, and why that support fails to discharge the
core obligation. Missing routine detail, a standard implicit step, or inability to find
a repair cannot support rejection. Challenge unnecessary revision requests too:
optional exposition or an already justified step is not a confirmed defect. Keep the
decision consistent across equivalent descriptions as omission or invalid inference.
Under this schema you cannot
rewrite the review or silently change its rejection flag: a disagreement withholds
the bundle for follow-up, rather than automatically converting it to revision.

Audit EVERY feedback candidate exactly as written, including diagnoses and repair
requests, not just optional suggestions. For each candidate evaluate:

1. `grounded_in_source`: its descriptions and cited context match the original inputs.
2. `addresses_validated_issue`: it responds to the actual reviewed issue rather than
   introducing a new allegation or imposing an unrelated requirement.
3. `preserves_problem`: it preserves the original assumptions, domain, quantifiers,
   and requested result; narrowing the problem is not a valid repair.
4. `mathematically_supported`: asserted mathematics and proposed fixes have adequate
   support. A request to prove something is not itself a claim that it is provable.
5. `uncertainty_calibrated`: it accurately distinguishes known error, missing support,
   tentative investigation, and verified repair, and makes no unsupported completeness claim.

Approve only if all checks hold. Otherwise withhold the entire text. Mixed good and bad
advice in one candidate is withheld as a unit. If the issue review is disputed or
uncertain, withhold every candidate. Explain each decision internally.

Return `FeedbackAudit` JSON with the exact input issue ID and exactly one decision for
each feedback ID. Do not return replacement text, a summary for the generator, or extra
feedback IDs. Your rationale is an internal audit record, not a release channel. A
needed rewrite must undergo a new audit outside this call.
