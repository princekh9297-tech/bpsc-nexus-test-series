# Question Import + Student Quiz Start Fix

## Fixes
- Improved question field detection across common JSON/CSV exports, including nested JSON payloads and punctuation/spacing variations in column names.
- Added semicolon, tab and pipe CSV delimiter detection, plus headerless CSV support.
- Added numeric option column support (`Option 1` through `Option 5`).
- PDF import now retries OCR when the embedded text layer exists but cannot be parsed into MCQs.
- Published FREE quizzes can now be started by regular Telegram users without demo-registration verification. Paid quizzes remain protected by the existing entitlement check; unpublished/private quizzes remain restricted.
- Removed the local profile-completion check from the student quiz-start button path. Profile details no longer block an otherwise permitted quiz attempt.

## Deployment
Replace these files in the existing repository:
- `miniapp.py`
- `question_bank.py`
- `static/index.html`

Keep the existing `requirements.txt`. For scanned PDF/image OCR, the server still needs Tesseract and (for PDF rasterization) Poppler installed in addition to the Python packages.

## Validation performed
- Python compilation: `miniapp.py`, `question_bank.py`
- JavaScript syntax: combined inline scripts from `static/index.html`
- Import smoke tests: TXT, semicolon CSV, headerless CSV, nested JSON, ZIP batch
- Live Telegram and database integration tests were not available in this environment.
