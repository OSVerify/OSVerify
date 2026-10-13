---
name: propose-solution-issues
description: Propose concrete mathematical issues and draft repair feedback for a supplied full candidate solution in the OSVerify workflow.
---

For `request.mode=partial`, the shared policy's Partial-progress mode section
overrides full-solution completeness, rejection, repair and handoff rules below.


Read [the shared review policy](../policy.md) and the call workspace's `input.json`.
The input contains the immutable question and full solution, numbered versions, an
assigned scope, and an optional `max_issues` (`null` means no count cap per invocation).
For a whole-solution scope inspect the complete argument, including whether the
question is resolved. For a segment scope focus on its verbatim `focus_text`, using
the before/after summaries as navigation. Read any original passage, search the web,
or perform read-only calculations as needed. You choose the investigation order.
You may report a related issue outside the focus span; cite its actual original lines.
Do not demand that a segment prove the entire question or mistake omitted context
in a summary for missing content in the solution. Verify summary-dependent claims
against the original. There is no obligation to find an issue in every segment.

Search for specific false claims, invalid inferences, calculation
errors, omissions, and useful minor corrections. Inspect theorem hypotheses, domains,
quantifiers, boundary cases, and transitions where relevant to this solution. Apply
the policy's investigation checks: whether every hypothesis of the target statement
is used, whether asserted objects are constructed and their properties proved, whether
similar-looking definitions agree exactly, and whether each external citation's
statement, hypotheses, and downstream use hold. Do not
produce a decomposition or invent a fixed number of findings.
Distinguish missing core obligations from routine implicit justification. Check the
full text before alleging incompleteness; optional extra exposition is not an error.

Each issue is one logical allegation against one target, with source spans, concrete
evidence, and its own ID. Prefer independently checkable allegations. Merge repeated
descriptions of the same allegation, but retain different allegations against the
same target separately. Avoid a large bundle whose evidence confirms only one part.

Draft useful feedback where warranted, keeping diagnosis and repair advice in separate
items with separate IDs. Suggestions are proposals, not approved advice. Cite the
required question lines and the relevant solution boundary for an omission.
When a concrete local correction is supported, include it and explain how it preserves
the affected downstream argument. Do not suppress a genuine issue because it is
repairable, or infer that a false auxiliary statement necessarily defeats the proof.
The validator will separately adjudicate the issue and the proposed repair.

Return `ProposalBatch` JSON matching the supplied schema. Use the input `scope_id`.
By default, list all distinct issues found; do not stop at an arbitrary count or
invent findings to meet a quota. If `max_issues` is a positive number, respect that
explicit cap and prioritize material findings. Set `search_status=incomplete` if
any count limit, context limit, unavailable evidence, or unfinished investigation
leaves known issues or planned search unfinished. Record limitations candidly.
An empty issue list is permitted when the planned review is complete.
