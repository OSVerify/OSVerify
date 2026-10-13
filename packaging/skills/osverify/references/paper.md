# Mathematical paper referee review

Review mathematical correctness within a selected scope. The report identifies
major/minor/unclassified audited issues without an overall solution verdict.

## Run the referee

Pass the paper itself; the engine copies it into the run and selects the scope:

```bash
uv run --script /absolute/skill/scripts/osverify.py verify \
  --review-type paper --paper /absolute/input/paper.tex --decompose \
  --request-id paper-v1 --run-dir /absolute/run
```

Without `--question`, `--targets` chooses what the review must establish: `smart`
(default) asks one model call for the main results and the key steps they rest on;
`lazy` takes the results announced in the introduction; `manual --labels thm:main
lem:key` extracts those labeled environments with no model call; `full` reviews the
whole paper. Selected statements must be verbatim
source excerpts. If the user supplies a review question or scope, save it in the run
directory and pass `--question /absolute/run/scope.md` instead of `--targets`. The
selected scope is written to `paper-inputs/question.md` (or the full-paper scope);
report which scope was used. Rerun the same command to resume; a changed paper
needs a new run directory.

For PDF-only input use [pdf.md](pdf.md): pass the PDF as `--paper`, never text you
extracted yourself, because that bypasses the page checks and per-issue source gate.
A verified corresponding native source uses the native path above.

Paper review completes the selected scope by default. --early-stop explicitly stops
after a confirmed blocking issue. Do not use --mode partial for papers; partial is
a solution mode, while an unfinished paper run is an execution state.

The proposer, validator and feedback auditor each receive the literature tools.
Follow [literature.md](literature.md) for unresolved references and source evidence.

## Deliver the report

Read the detailed internal report.json/report.md, scope extraction, coverage and classification
status. Internal solution decisions in .verification/ are not paper-level verdicts.
Preserve unknown severity. Release only approved feedback text, without adding
unaudited explanations or repairs. Review completion is not a correctness certificate.

Default template reports may make extra writing/audit model calls. PDF generation
needs XeLaTeX or LuaLaTeX. --no-report skips automatic PDF postprocessing but retains
report.md/report.json and severity classification. Disclose report-generation failures
separately from review completion and link only artifacts that exist.

### Reader-facing PDF

The PDF begins with Review summary and a compact table distinguishing completed
review from partial review. Internal scope counts are review units, not pages or
theorems, and stay in report.json. Each Major issue / Minor issue has a precise
paper quotation and approved feedback; unclassified issues stay unclassified.
Reference limitations are briefly summarized as the result not fully checked and
its actual reason, separately from mathematical findings. Overlapping reference
records may be grouped with all source indices retained and independently checked.
Raw execution/audit fields, severity rationales, assessment tags and job paths stay
in the internal source/report records. Do not paste them into the public overview.
The report writer and independent editorial checker must both use the updated
schema. A new format-version directory preserves historical PDFs. Older frozen
runs retain their original runtime; regenerate into a separate output directory.

Use the title `Referee Report on “<paper title>”` and bold paragraph labels.
Suggestions use smaller gray type. Typeset mathematical expressions as LaTeX;
replace internal line numbers with verified printed sections, theorem/proof numbers
and precise paragraph descriptions. The writer records each presentation substitution
against immutable source text, and the independent checker approves its fidelity.
Deliver the resulting PDF, not a raw dump of the internal report.md. A quota-exhausted
snapshot keeps this layout and Partial status, but preserves exact unsummarized
feedback/reference notes when editorial model calls cannot finish. It must say so.

The report heading uses the paper title without a legacy task/version date suffix.
The renderer records the report generation date (UTC) in the small gray closing note;
do not invent a date or add it to the overview. A short final references section
may naturally continue onto a new page; this alone is not a layout defect.
