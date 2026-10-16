# PDF reader fixtures

These small, synthetic documents exercise the reference reader without network
access or a model call. They contain no user or third-party research content.

- `reference-text.pdf`: two pages generated with ReportLab, with an embedded font
  so rendering does not depend on the host font installation.
- `reference-scan.pdf`: one page saved by Pillow from an RGB image, with no text
  layer, to exercise the original-page fallback.

Real-Poppler tests check extracted text, rendered pixels, and source hashes.
They are skipped when Poppler is absent; Ubuntu CI installs `poppler-utils`.
