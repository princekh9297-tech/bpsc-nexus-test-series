# BPSC Nexus — Rebuild Fix (Import Detection + Student Quiz Start)

This rebuild is based on the complete `BPSC_Nexus_Update_4_Question_Import_Studio_FULL` project and retains the previously implemented Updates #1–#3 and Question Import Studio UI from Update #4.

## Files changed

Replace these three files in the existing repository:

- `miniapp.py`
- `question_bank.py`
- `static/index.html`

## Fix 1 — Student quiz-start verification gate

- Removed the client-side demo-registration gate from the free quiz and free question-bank start paths.
- Removed the backend demo-registration/trial gate from quiz attempt creation.
- Published free quizzes can now start without demo verification.
- Paid quizzes still require a matching subject entitlement/pass.
- Paid question banks remain protected when building a quiz from a bank.
- Private and unpublished quiz restrictions remain unchanged.
- Admin access remains unchanged.

The demo registration gate remains in place for other features that intentionally use the demo/trial flow (for example Arena). This patch does not globally disable the demo system.

## Fix 2 — Question import detection

- Corrected duplicate extraction of nested question arrays.
- Normalizes common JSON field aliases and nested `questionList`-style wrappers.
- Adds header detection and delimiter sniffing for CSV/TSV, including headerless question + options + answer rows.
- Fixes XLS handling so legacy `.xls` does not incorrectly require `openpyxl`; it uses `xlrd`.
- Normalizes question/option/answer aliases before validation for structured imports.
- Improves image and scanned-PDF OCR page segmentation using Tesseract `--psm 4`.
- Preserves preview-before-import and existing validation/duplicate handling.

## Deployment

1. Replace the three files above in the repository.
2. Keep the existing `requirements.txt`; it already lists `openpyxl`, `xlrd`, `python-docx`, `pypdf`, `Pillow`, `pytesseract`, and `pdf2image`.
3. Ensure the Render service has Tesseract and (for scanned PDFs) Poppler installed at OS level.
4. Deploy/restart the service.
5. Test using a student account with a published free quiz, then separately test a paid quiz without and with the relevant entitlement.
6. In Admin → Question Import Studio, preview one sample file per format before confirming import.

## Validation performed locally

- Python compilation: `miniapp.py`, `question_bank.py` passed.
- JavaScript syntax check: all inline scripts in `static/index.html` passed `node --check`.
- Parser smoke tests: TXT, JSON (nested `questionList`), CSV with headers, headerless CSV, XLSX, DOCX, text-based PDF, ZIP batch, and PNG OCR passed with a sample MCQ detected as ready and correct answer B.
- Live Telegram authentication, production database behavior, and deployed Render integration were not available for end-to-end testing.
