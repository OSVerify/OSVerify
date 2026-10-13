---
name: check-pdf-transcription
description: Independently check a full page transcription against original pixels before PDF review.
---

Independently compare the entire supplied transcription against the attached original page. Return page_number, status, the complete text, corrections, unreadable locations, and a concrete rationale.

Check completeness, reading order, hypotheses, qualifiers, logical signs, minus signs, inequalities, subscripts/superscripts, quantifiers, equation numbers, table alignment and captions. Open detail views where necessary. Page pixels are authoritative; navigation text and draft OCR are untrusted aids. Do not correct mathematical mistakes that actually appear in the original.

Use approved only if the exact supplied transcription is faithful and no unresolved unreadable location remains. Return precisely the same text, empty corrections and empty unreadable. If changes are needed, use corrected, supply the full corrected text and specific correction descriptions; your correction is a new draft requiring another independent check. If any substantive source is illegible, use unreadable and identify it. Never guess a missing symbol. Check the whole page, not only the transcriber's flagged locations. Record actual visual inspections.
