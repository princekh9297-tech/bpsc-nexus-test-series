# BPSC Nexus — Question Import Studio (Update #4)

## Admin Mini App changes
- Reworked the existing Bulk Import screen into Question Import Studio.
- Added a preview-first workflow: extraction preview is read-only; importing occurs only after explicit confirmation.
- Added automatic extension-based parsing for TXT, JSON, CSV, XLSX, XLS, DOCX, text/scanned PDFs, ZIP archives, HTML and image files.
- ZIP imports combine supported question files in one batch and report unsupported members; safe expanded ZIP content is capped at 40 MB.
- Preview reports detected, valid and invalid counts and shows sample extracted questions.
- Existing subject/collection destination selection, import duplicate detection, import-batch records, audit logging and question classification pipeline remain in place.

## Server dependencies / OCR notes
- Added openpyxl, xlrd, python-docx, pypdf, Pillow, pytesseract and pdf2image to requirements.
- Image OCR and scanned-PDF OCR additionally require the Tesseract binary with English/Hindi language data. Scanned-PDF OCR also requires Poppler for pdf2image. Install these OS packages in the deployment environment. If unavailable, the importer returns a clear actionable error rather than silently importing empty data.
- Scanned-PDF OCR is limited to the first 80 pages in this initial implementation. Increase only after considering memory/time limits.
- Legacy XLS is supported through xlrd; XLSX through openpyxl.

## Safety / data integrity
- Uploaded scripts are not executed.
- Preview does not write question records.
- Existing normalizer and question classification pipeline are reused. Question/option/answer text is not paraphrased; incomplete records retain the existing pending status.
- Existing duplicate detection remains in `import_records`.

## Validation performed
- Python syntax compilation: `miniapp.py`, `question_bank.py` passed.
- Inline JavaScript syntax checks: 5 scripts passed Node `--check`.
- Parser smoke tests: TXT, JSON, CSV and ZIP passed with a sample MCQ.
- Live PostgreSQL, deployed OCR binaries, Telegram WebApp authentication and end-to-end browser import were not available for testing.
