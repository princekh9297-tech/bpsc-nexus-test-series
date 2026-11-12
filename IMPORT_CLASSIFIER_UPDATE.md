# BPSC Nexus — Intelligent MCQ Import Integration

## Changed files
- `question_bank.py`: ingestion-time MCQ classification, confidence/source/review metadata persistence, optional AI fallback.
- `miniapp.py`: invokes the AI fallback during preview and final import, returns classification diagnostics.
- `static/index.html`: import preview displays detected layout, confidence, source, and review-needed state.

## Behaviour
1. Every imported question is classified before it enters the Question Bank.
2. The deterministic engine recognises the 15 configured layout families and preserves original question text, options, answer keys and explanations.
3. Classification metadata is stored with the question: `question_type`, `classification_confidence`, `classification_source`, `classification_review_required`, and `original_question_type`.
4. Low-confidence classifications are sent to the existing Question Intelligence review queue.
5. If the existing Nexus Intel OpenAI-compatible provider settings are configured, the AI fallback is invoked only for low-confidence questions. It may return classification metadata only; it cannot rewrite question content or answers.
6. If AI settings are absent or the provider fails, deterministic classification remains in place and uncertain questions stay in the review queue.

## Existing environment variables
The AI fallback reuses these existing variables (no new secrets required):
- `INTEL_AI_BASE_URL`
- `INTEL_AI_API_KEY`
- `INTEL_AI_MODEL`

The base URL should be the OpenAI-compatible API base, with `/chat/completions` appended by the application. If these are not configured, all uploads still use deterministic classification and ambiguous questions are queued for admin review.

## Deployment
Replace the three changed files in the repository, commit, and redeploy:
- `question_bank.py`
- `miniapp.py`
- `static/index.html`

No database migration command is needed; the Question Bank schema setup adds the classification columns with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.

## Validation performed
- Python compilation for `question_bank.py` and `miniapp.py`.
- JavaScript syntax check for scripts in `static/index.html`.
- Local classifier tests covering all 15 configured layout types.
- Ambiguous-question review-queue test.
- Mocked AI fallback test confirming only classification metadata is updated.
- ZIP integrity checks.

Production Telegram/database integration still requires deployment and a live test import.
