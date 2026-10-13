---
name: validate-solution-issue
description: Independently adjudicate one proposed issue against a full mathematical solution and prepare feedback candidates for a separate exact-text audit.
---

For `request.mode=partial`, the shared policy's Partial-progress mode section
overrides full-solution completeness, rejection, repair and handoff rules below.


Read [the shared review policy](../policy.md) and `input.json` in the call workspace.
You receive original and numbered inputs and one proposed issue, including untrusted
feedback suggestions. Your output is an `IssueReview`, not the released report.

First reconstruct the challenged claim as written and used. Check the surrounding
argument, eligible assumptions, later corrections, and whether the issue actually
affects a required result. Try to refute the allegation before confirming it. Evaluate
the evidence yourself; do not substitute a related but different error. If the original
allegation fails but suggests another issue, do not silently relabel it as confirmation.
When the issue concerns a cited external result or a definition taken from a source,
locate and read the source yourself, compare its exact statement, definitions, and
hypotheses with the submission, and check the deduction the submission draws from
it, following the policy's citation procedure. If the source cannot be located, the
issue may stay `unresolved`; a missing source does not prove the claim false.

Decide realness, kind, and impact separately. Apply the shared policy's distinction
between a local omission and demonstrated failure to supply a core obligation in the
full submission. Check original context and legitimate implicit justification;
neither missing detail nor inability to verify it establishes core incompleteness.
Missing support does not prove falsity. Before supporting
rejection of a confirmed blocking error, check for a supported local repair under the
shared policy: a justified correction that preserves the problem and reconnects the
affected downstream uses within the existing proof route. Check what an auxiliary
claim actually needs to establish; a false stronger version may admit a sufficient
weaker replacement.

When such a repair is supported, keep the original issue `confirmed`, use its accurate
kind and impact on the unrevised text, but set `rejection_supported=false`. Explain
the concrete correction, mathematical support, and affected downstream uses in
`rationale` and `evidence`; prepare revision feedback for the exact-text audit. Do not
mark the issue `not_real` just because it can be fixed. If supporting rejection instead,
explain the concrete error and its blocking role, including why any proposed local
repair does not resolve it. Speculation is not a verified repair, and failing to find
a repair is not proof of an error or a missing core obligation. For an omission-based
rejection, identify the required result, the point where the submitted argument stops
short, and why its actual support is insufficient after the full-context check.
Minor issues cannot reject. Do not turn optional exposition into required revision.

Review the proposer's feedback independently from the issue. Drop or correct advice
that changes the problem, relies on an unproved claim, or overstates what a repair
achieves. Return a self-contained `feedback_candidates` list; its texts will be audited
in another conversation. Keep diagnosis, repair request, and optional remedy separate
so one bad suggestion need not prevent release of an accurate diagnosis.

A confirmed issue needs source spans and at least one diagnosis candidate. A `not_real`
issue has no feedback candidates. An `unresolved` issue may have only uncertainty-aware
clarification requests. Use unique feedback IDs and preserve the input issue ID.
Give sufficient evidence and reasoning for independent review without writing a new
solution. Return exactly the supplied JSON schema.
