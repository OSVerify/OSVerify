---
name: segment-solution-text
description: Segment an AI/ML paper verbatim with descriptive claim and evidence context.
---

Read [the AI/ML paper policy](../policy.md), input.json, question.md and
numbered-solution.md. candidate-solution.md is the exact unnumbered original.
Return ordered contiguous original line ranges covering the entire assigned scope,
in one pass. Choose natural sections or argument/experiment units with no fixed
count or balancing requirement. Preserve all text, including tables and appendices.

For each range, context_before describes relevant earlier claims, definitions and
settings; context_after identifies actual later evidence, qualifications and uses.
Help the downstream reviewer connect claims to methods, tables, proofs and appendix
details across sections. Preserve modality and conditions. These summaries describe
the paper, never certify its content or create new obligations. Do not treat later
results as earlier proof premises. Do not silently drop text because it looks like
related work. Do not judge novelty, correctness or propose issues. The proposer has
full original-source access and is not limited to the segment's evidence.

Return only TextDecomposition matching the supplied schema.
