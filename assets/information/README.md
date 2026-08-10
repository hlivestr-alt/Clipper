# Product Information Sources

Place approved, searchable `.pdf` and `.docx` product documents in this folder. Subfolders are supported. The pipeline and Variants preview use these sources for evidence-backed Ingredients, Benefits, Usage, and CTA text.

For reliable matching, give each product section a clear heading such as `PROYA TONER`, followed by labeled sections such as:

- `Ingredients:` or `Kandungan:`.
- `Benefits:` or `Manfaat:`.
- `How to use:` or `Cara pakai:`.
- `Description:` or `Deskripsi:`.
- `CTA:` or `Rekomendasi:`.

`product_information.py` extracts PDF text and DOCX paragraphs/tables, fingerprints each source, and writes the cached index to `working/product_information_index.json`. With `PRODUCT_INFORMATION_LLM_ENABLED=True`, LM Studio classifies only source-supported text; an unavailable/invalid LLM response falls back to deterministic rules and records a warning. Conflicting or ambiguous facts are excluded from eligible generated text and surfaced as diagnostics.

Every fact retains its original source file and locator (PDF page/line or DOCX paragraph/table row), extraction method, and conflict state. Dynamic-text selections and rendered manifests retain public evidence metadata rather than inventing claims.

Scanned image-only PDFs are not OCR processed. Convert them to searchable PDFs or approved DOCX files before use.

Use the Variants page **Assets & diagnostics** tab to inspect status and force a rescan, or call `POST /api/product-information/rescan` through an authenticated Clipper client. Editing a source automatically changes its fingerprint and triggers re-indexing on the next load/scan.
