# BPSC Nexus Admin Panel — Bulk Delete Fix

## What changed
- The Question deletion screen now has button-driven bulk actions: delete all questions, all Ready, all Pending, or all Archived.
- Each action first counts the matching rows and presents an explicit confirmation button with the count.
- Confirmed bulk deletion removes quiz links and answer-level attempt rows for only the matching questions, then deletes those question records in one database transaction.
- The original ID-based bulk delete remains available when you want to remove a specific subset.
- No database tables are dropped and no schema migration is required.

## Deploy
1. Back up your GitHub repository.
2. Replace only `question_bank.py` with the included file (keep the existing `bot.py` from your current deployment if it already routes `qb:` callbacks to `question_bank.handle_callback`).
3. Commit and deploy the bot service.
4. In Telegram, open the private admin panel → Question Bank & Quiz Studio → Delete Questions.
5. Choose a bulk-delete button, review the matching count, then press the explicit permanent-delete confirmation.

**Warning:** Bulk deletion is permanent. `ALL questions` deletes every record in `question_bank`. Prefer a status-specific action if that is what you intend. Quiz links and answer-level attempts for deleted questions are removed; aggregate attempt totals remain.
