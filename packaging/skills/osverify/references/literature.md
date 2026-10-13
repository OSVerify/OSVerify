# External literature during paper review

New native-text and checked-PDF reviews expose the per-call `osverify_literature`
MCP server to proposer, issue validator and feedback auditor. It requires no global
MCP setup. Each role must inspect the material supporting its own judgment.

- `search_literature`: find bibliographic candidates; metadata is not inspected text.
- `resolve_reference`: retrieve a public URL, DOI or arXiv ID. It supports HTML,
  PDF and LaTeX, follows bounded scholarly full-text links, and preserves explicit
  arXiv versions. Check title, authors and version before substituting a source.
- `read_reference`: inspect passages and requested original PDF page images.

Keep dependency checking separate from access: `available`/`fulltext` means readable
material, not a verified theorem or a justified application. Record a stable
`dependency_id`, exact locator/version and inspected `source_evidence`. An alternate
source may resolve the same dependency, while the failed URL remains in history.
One role's successful check does not clear another role's uncertainty. Do not report
paywalls, TLS failures, 404s or unresolved dependencies as mathematical defects.

The download defaults are 20 seconds without network data and 300 seconds total;
incremental reads allow slow progressing PDFs beyond the old 75-second deadline.
The byte limit remains 32 MiB. Tool configuration preserves the policy in receipts
and allows bounded extraction/rendering time. The outer model-call timeout is
separate (`--timeout-seconds`, normally 900); increase it explicitly for a new run
if needed. Do not change a frozen run's settings or retry indefinitely.

Retain source receipts, per-role tool journals and reference observations. Inspect
`reference_limitations` in the public report for unfinished dependencies. A completed
retrieval test does not establish mathematical review quality or complete coverage.
Old run directories and legacy caches remain frozen; use a new run for these tools.

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

New stores use version 3. The new code refuses writes to prior version-2 stores,
so earlier fulltext classifications cannot silently carry into a fresh-policy run.
Use a new run directory; resume frozen runs with their original pinned runtime.
