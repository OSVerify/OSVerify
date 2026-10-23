# Literature tools for new paper reviews

New reviews started from the development source expose an invocation-scoped
`osverify_literature` MCP server to the proposer, issue validator and feedback
auditor. It is configured on that call's command line; user/global Codex settings
and account configuration are unchanged. Ordinary solution calls and editorial,
severity, segmentation and deduplication roles do not receive it. A solution review
started with `--references` instead gives the same three roles a separate
`osverify_supplied_references` server that reads only the run's frozen supplied files
(see the [output contract](output-contract.md#supplied-reference-files)).

- `search_literature(query, limit=5)` returns Crossref metadata candidates. It does
  not identify the correct work automatically or assert access to the full text.
- `resolve_reference(reference)` accepts a public URL, DOI or versioned arXiv ID,
  retrieves content, and follows up to four explicit scholarly download links
  within a five-response budget. Unavailable DOI sources may return OpenAlex OA
  location candidates; the reviewer chooses and verifies an appropriate version.
- `read_reference(reference, query='', page=None, start_line=1, max_lines=80)`
  returns original text with locations, source hashes and paths. `page` is a
  one-based PDF page, and also returns its rendered pixels. Queries are literal
  text matches, not semantic claims that a theorem is absent.

The tools retrieve public material into the run's shared reference cache. They
cannot execute shell commands, read arbitrary files, change the candidate, or
access another role's judgments. HTTPS certificate validation and public-address
checks (including redirects) remain enabled. Login and subscription restrictions
are not bypassed. The MCP process uses only the Python standard library and the
existing OSVerify dependencies, so no separate MCP package is required.

## Download timeouts

Source retrieval defaults to a 20-second socket idle timeout and a 300-second
total download budget across the bounded landing-page traversal. Receiving data
allows the download to continue beyond the former 75-second limit; a stalled
read still times out. Reads return after an underlying buffer read so a slowly
trickling stream cannot indefinitely hide elapsed time while filling a 64 KiB
buffer. The total budget is checked between reads (an in-flight read can take
up to its socket timeout). The 32 MiB size limit remains unchanged.

Configure a store with `ReferenceStore(path, idle_timeout_seconds=20,
download_timeout_seconds=300)`. The role backend forwards the values to its
MCP server via `--idle-timeout-seconds` and `--download-timeout-seconds`.
Receipts and backend identity record the policy. Same-URL cache waits and the
MCP tool-call budget include download and PDF processing time. The enclosing
model-call timeout remains an independent outer limit; it is not changed here.

## Content and evidence

HTML processing distinguishes scholarly text, ordinary web pages,
navigation/abstract landing pages, and access challenges. Project pages, personal
homepages and result announcements can be read without paper-style headings.
They carry `source_kind=web_page`; this does not establish that an underlying
paper or proof was retrieved. Markdown, plain text and source-code files carry
`source_kind=text_file`; code is read as text and never executed. GitHub `blob`
links resolve to their raw files. Binary data is not accepted as text merely
because its URL ends in `.md` or its server declares `text/plain`.

Abstract-only pages, journal catalogues, access challenges and empty JavaScript
shells remain unresolved. Explicit scholarly download links are followed to the
paper rather than treating the landing page as its full text.
TeX math annotations, MathML `alttext`, formula
image descriptions, and structural MathML are preserved where available. PDF
extraction and page rendering continue to use Poppler. Extraction is fallible:
read the original PDF pixels when notation matters. A readable representation
is not proof that every required passage was retrieved or interpreted correctly.

PDF text extraction and page rendering fail independently. If `pdftotext` is
missing, times out or produces no text, the reader tries original page images
(first page by default; request other pages explicitly). A failed page render
does not discard readable text or other successfully rendered pages. Image-only
reads return pixels even when a literal text query has no match; this is not a
successful text match. Extracted text is decoded as UTF-8 on all platforms.

### Diagnose an unavailable source

Inspect `references/<url-hash>/receipt.json` and its immutable attempt receipts.
The `retrievals` entries record the requested/resolved URLs, HTTP status when
available, downloaded bytes and the error category:

| Category | Meaning |
| --- | --- |
| `metadata_only` | A page was downloaded, but contains only navigation/metadata or insufficient readable content. |
| `unsupported_format` | Download succeeded, but the body has no supported readable representation. |
| `access_challenge` / `access_denied` | A browser challenge or HTTP permission failure prevented reading. |
| `dns`, `tls_certificate`, `timeout` | Address resolution, certificate or connection/download failure. |
| `truncated` | The server did not deliver the complete declared response body. |
| `pdf_processing` | PDF download succeeded, but neither text extraction nor page rendering produced readable material. |

For PDFs, `processing_errors` records the stage (`text_extraction` or
`page_render`), tool, requested page when applicable, and cause (`tool_missing`,
`timeout`, `tool_failed`, or `empty_output`). Nonzero tool exits retain their code
and a bounded stderr excerpt. These diagnostics can coexist with readable text
or images. `read_reference` includes them and reports `page_image_status` so a
missing image cannot look like a successful render. Earlier receipts retain
errors even if a later cached-page retry succeeds.

If a tool is missing, run `pdftotext -v` and `pdftoppm -v` in the same environment
that launches OSVerify. For Ubuntu/Debian under WSL, install `poppler-utils`
inside WSL (`sudo apt-get install poppler-utils`); a Windows installation is not
necessarily on the WSL process's PATH.

These distinguish retrieval failures from an MCP startup or tool-process failure.
Share the failed URL and the relevant receipt/error when reporting a problem;
raw model traces can contain private paper text. File locks use `flock` on Unix
and `msvcrt.locking` on native Windows. Windows transport and provider behavior
still depend on the local Codex installation.

An access receipt describes a URL and each retrieval attempt. A dependency check
describes a mathematical obligation in the submitted paper. These have separate
states. A failed original URL does not force a dependency to remain unresolved if
the reviewer independently inspects an appropriate alternative source and records:

- a stable `dependency_id` tied to the original paper location and obligation;
- `source_evidence`: actually inspected source URLs, exact locators and versions;
- its own `verified` or `unresolved` assessment and explanation.

Here `verified` records a completed reference check, not that the paper is correct;
a discovered mismatch still follows the ordinary issue-validation pipeline.
Checks are merged by explicit dependency ID within the same review context.
Legacy checks retain their exact URL/dependency identity. A proposer assessment
does not clear a validator/auditor's unresolved check. Missing evidence is never
converted into an error in the paper. Existing issue validation and exact-feedback
auditing remain mandatory.

## Cache and frozen runs

New stores use version 5 for independent PDF text/image fallbacks and processing diagnostics. Earlier stores must use
their original runtime; this update does not reinterpret old retrieval results.
Source bodies and per-attempt receipts are
append-only; `receipt.json` is an index to the latest attempt. Later page requests
add a new view directory instead of changing earlier PDF/text/image artifacts.
Per-URL process locks deduplicate concurrent fetches without serializing unrelated
sources. Final reference observations and tool journals are included in job hashes.
All literature implementation modules are included in backend provenance.

Do not update historical runtime snapshots to resume a run.
Existing runs must resume with their original runtime. The new fetcher refuses
writes into a legacy store. Start a new run to use the new tools. Historical source snapshots and legacy skill runtimes remain unchanged. Only the
current distributable review-runtime is refreshed for new runs.

## Replaying failures

Run from the updated source checkout (the output directory must not exist):

```sh
PYTHONPATH=src uv run python experiments/replay_reference_failures.py \
  --manifest /path/to/osverify-reference-failures-20261009.json \
  --output /path/to/new-literature-replay --workers 4
```

The replay freezes a deterministic, domain-diverse sample and implementation
hashes, preserves current raw responses and receipts, reports recovery by failure
category, and verifies original receipt hashes afterward. Historical failures and
live successes can reflect network/site changes. The separate same-response HTML
comparison identifies cases the former PDF/LaTeX-only branch would reject today.
These measurements establish retrieval behavior, not mathematical correctness.

## Initial validation (2026-10-10)

A live replay sampled 50 of the 194 failed host-fetch receipts, across failure
categories and domains. The final HTML implementation recovered readable material
for 19/50, including 17/30 format failures. Fourteen returned HTML representations
would be rejected by the former format branch on those same response bodies; one
successful retrieval followed a landing-page download link. Network/site changes
also contributed, so 19/50 is not an isolated causal estimate of this patch.
All 576 original receipt hashes remained unchanged.

A separate small live-model integration review completed proposer, issue validator
and feedback auditor, each with its own successful `read_reference` call. This
establishes tool connectivity and review plumbing, not broad mathematical quality.

## Native web-search fallback

Keep the current literature tools. A failed resolve/read returns a structured
`fallback` with the inherited `live`, `cached`, `disabled` or `unavailable` mode.
Crossref failures also return this plan instead of ending the tool call with an
unstructured error. Proposer, validator and auditor use their existing native web
search tool, within the same role call: at most two queries and three candidate
retrievals per dependency. No nested model process or extra API key is introduced.
Explicitly disabled/unavailable search remains disabled/unavailable.

Search the original bibliography title, authors and version, never the title of a
redirected journal catalogue. Register actual results with
`search_literature(query=actual_query, web_results=[{"title": "...", "url": "..."}])`.
This optional path records caller-supplied candidates and does not itself search
the web. Its `search_id` binds the query/results receipt; pass it to
`resolve_reference(reference=url, search_id=search_id)` and then read the source.
The native search events, candidate registration and retrieval journal together
preserve discovery provenance. Registration alone cannot authenticate a search,
match source identity, certify full text, or clear an unresolved dependency.

HTML body classification excludes abstracts, contents, navigation and bibliography
regions. Journal/product catalogues do not qualify merely because their summaries
contain mathematical keywords; issue-page download buttons are not followed as if
they belonged to the originally requested work. Classification reasons and body
character counts are retained in navigation and access receipts. The full raw HTML
is still saved for inspection. Extraction remains heuristic; verify title, authors,
version and the actual required passage independently.

Store-version isolation prevents earlier fulltext classifications from silently
carrying into a fresh-policy run. See [Cache and frozen runs](#cache-and-frozen-runs)
for the current version; resume frozen runs with their original pinned runtime.

## Catalogue and fallback validation (2026-10-10)

A paired replay of 47 saved HTML responses corrected seven catalogue/abstract
false positives, retained six genuine HTML bodies, and recognized two additional
challenge responses. These are response counts, not distinct-paper counts. All
eight previously accepted sources (six HTML and two PDF) remained readable with
identical extracted text when replayed from the same bytes.

A fresh online replay of the same 23 failed URLs, representing 18 works/pages,
retrieved 6/18 through direct access and Crossref. The existing native web-search
fallback added three, reaching 9/18. The caller selected candidates from actual
search results; this is an observed tool-chain result, not an autonomous ranking
benchmark or a full reference-correctness review. The previous online baseline
had reached 8/18 before fallback: a new access challenge and a Crossref 429 explain
the two missing works in this run, while paired PDF replay succeeded. This does
not establish that the new version is uniformly better under live conditions.

The three additional sources were an Atiyah--Singer III scan, Donaldson's Floer
monograph, and Scholze's p-adic Hodge erratum. The scan required an explicit page
image read after text search failed. All 5,973 protected files from earlier runs,
comparisons and legacy runtimes retained their hashes. Access restrictions, TLS,
404s, rate limits and source-identity checks remain unresolved where applicable.
