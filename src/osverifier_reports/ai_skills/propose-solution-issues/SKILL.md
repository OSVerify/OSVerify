---
name: propose-solution-issues
description: Propose atomic correctness issues in an AI/ML paper, grounded in its claims and evidence.
---

Read [the AI/ML paper policy](../policy.md) and input.json. The question is the
selected review scope; the candidate solution is the full paper. Review correctness
only, never novelty, significance, or publication value.

For whole-paper scope map each selected claim to its actual evidence, evaluation
settings, assumptions and limitations. For a segment follow its relevant claim and
evidence across the full paper; before/after summaries are navigation, not premises.
Check theory, methods, comparisons, statistics, evaluation, numerical consistency,
attribution and generalization where the actual claims require them. Use original
text, available tables and appendices. Do not infer missing figure or code contents.

Each issue must be one independently checkable allegation with original source
spans, the exact affected claim, and concrete evidence. Describe whether it concerns
a false assertion, invalid inference, demonstrated evidence gap or unresolved
question. Explain why any requested evidence is necessary for this claim. Check
that it is not already supplied. Keep distinct allegations against the same table
or claim separate. Draft diagnosis and advice separately; do not fabricate repairs
or equate absent support with falsity. Optional improvements alone are not issues.

Return ProposalBatch with the assigned scope_id. No arbitrary issue count or
minimum number is required; honor an explicit max_issues. `search_status` describes
whether you finished the planned search of the supplied, accessible evidence. Use
`incomplete` for an issue cap, unfinished reading/investigation or a context/time
limit that prevented that search. An empty completed batch is valid.

Unavailable supplementary material, code, data, logs or video belong in the separate
`evidence_limits` array, using only the schema's categories. Include a category only
when it actually limited this review. Such access limits, inability to reproduce an
experiment, unresolved allegations, and readable images missing from text extraction
do not by themselves make the search incomplete. Review the actual attached images;
use `unreadable_image` if an attached image cannot be interpreted. Preserve uncertainty
in the relevant allegation rather than claiming that unavailable evidence is absent.
