# Question Import Studio — Root Cause Fix

## Root cause

The Mini App admin import endpoint iterated through `aiohttp` multipart form parts, saved the uploaded `BodyPartReader` object, and only read it after the loop. Multipart parts are sequential streams. Advancing the multipart reader to a later field drains the previous part, so the retained file reader returned empty bytes. This affected every file extension regardless of whether its parser was correct.

## Fix

`miniapp.py` now reads the uploaded file bytes immediately when the `file` part is encountered, before advancing to the next multipart field. The handler then validates and parses the captured bytes after the form has been read. This preserves the existing parser, preview/confirm workflow, admin authorization, file-size limit, import history and database behavior.

## Validation

- Python compilation: `miniapp.py` and `question_bank.py`.
- Parser smoke tests: TXT, JSON, CSV with headers, headerless CSV and ZIP containing TXT + JSON.
- The parser tests confirmed non-empty records and expected question/options fields.
- Production Telegram authentication and live database import still require deployment verification.

## Deployment

Replace `miniapp.py` in the existing repository. No database migration or dependency change is required for this correction.
