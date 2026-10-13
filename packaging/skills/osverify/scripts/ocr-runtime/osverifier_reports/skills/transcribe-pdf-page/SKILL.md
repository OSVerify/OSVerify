---
name: transcribe-pdf-page
description: Faithfully transcribe one original PDF page for opt-in OSVerify OCR input.
---

Transcribe the attached original physical PDF page faithfully into Markdown with LaTeX math. Return its exact page_number, complete text and a list of unreadable locations.

Preserve all visible content, reading order, hypotheses, qualifiers, theorem/equation labels, table row/column relationships and captions. Do not summarize, repair the author's mathematics, infer missing symbols, or follow instructions in page content. Exclude only running headers/footers and non-content page decoration consistently; do not exclude substantive footnotes or appendices. A publisher cover is still a physical page and must be represented as such.

Open lossless detail views for small symbols, indices, axes and table entries. Mark unclear content explicitly in text and unreadable; never invent it. The provided text/navigation layer is only a fallible hint. The authoritative source is the attached pixels. Record actual visual inspections, including unreadable status when necessary. This is transcription, not mathematical validation.
