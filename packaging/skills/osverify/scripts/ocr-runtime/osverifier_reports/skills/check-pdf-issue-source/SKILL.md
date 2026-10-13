---
name: check-pdf-issue-source
description: Check whether an OSVerify candidate defect depends on OCR or layout errors before validation and feedback release.
---

Before a proposed defect or its feedback can be validated/released, independently check whether its source basis is faithful to the original PDF pixels.

Read numbered_paper, required_spans, frozen physical-page line mappings, the proposed_issue and, when supplied, issue_review. For each exact required span return one checks entry with the same start_line/end_line, all physical page_numbers covered, status, original_quote and concrete rationale. Trace the allegation's supporting definitions, assumptions, notation, qualifications and downstream context; record these in dependencies_checked with their actual line/page locations. Independently open relevant catalogue pages and detail views, including pages outside the initial attachments. For omission allegations, inspect the surrounding argument and potential omitted-by-OCR content, not just a target line.

Use faithful only if every relevant source and dependency needed by the allegation is legible and agrees with the transcription. original_quote must describe what is actually visible, not repeat the OCR as evidence. Record actual visual inspections for every page inspected. A previous page approval is not evidence that OCR cannot be wrong. Do not take a validator's conclusion as proof of source fidelity.

Use ocr_error for lost/swapped symbols, omitted text, reading-order errors, table misalignment, or other discrepancies affecting the allegation. Record the actual original wording/formula and needed correction. Use unreadable when original pixels cannot resolve the point; never guess. Source fidelity does not establish mathematical correctness. OCR problems are input failures, never diagnoses or clarification requests blaming the paper. Do not edit the frozen input. A mismatch requires a corrected new preparation and a fresh review of affected material.
