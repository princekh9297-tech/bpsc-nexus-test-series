# BPSC Nexus manual payment update

## Included
- V12 bot/admin panel and question_bank.py retained.
- V8 Mini App API/frontend and group_quiz.py included for a complete deployable folder.
- PhonePe QR at `static/phonepe_qr.jpg`; membership page at `/membership`.
- Manual UPI payment requests with screenshot/reference, admin pending list, approve/reject, and All-Access activation.
- Fixed All Access prices: 60d ₹90; 90d ₹130; 120d ₹150; 180d ₹180; 249d ₹220; 365d ₹300.

## Render deployment
1. Back up current repository and confirm the bot and Mini App services share the same `DATABASE_URL`.
2. Deploy this folder with the existing bot service start command and Mini App service start command (`python miniapp.py`). The bot and Mini App may be separate Render services pointing to this same code repository/folder.
3. Keep existing `BOT_TOKEN`, `DATABASE_URL`, `RENDER_EXTERNAL_URL`, `WEBHOOK_SECRET`, `ADMIN_TELEGRAM_IDS`, and `MINI_APP_URL` environment variables. Set `MINI_APP_URL` to the HTTPS root URL of the Mini App service.
4. Database migration is additive (`CREATE TABLE IF NOT EXISTS` + index only); it does not drop/reset existing tables.
5. In Telegram, open `/admin` → **Payment Requests**. Open a pending request to receive the screenshot in admin chat. Check the credit in PhonePe/bank before Approve. Approval activates All Access; rejection grants nothing.

## Important checks
- Test with a small test payment before public launch.
- Keep `ADMIN_TELEGRAM_IDS` limited to trusted admins.
- User must have pressed `/start` so their account exists in `app_users`.
- Static QR may require payer to enter the amount manually.
- Screenshot is evidence for review only, not automatic payment confirmation.
- Battle Arena tables and handlers were not changed.

## Limitations in this build
- User-facing self-service purchase flow currently supports All Access duration plans. Existing individual question-bank catalogue/prices are not modified by this checkout screen.
- Admin can approve/reject requests but price editing is not yet provided in the panel; the six confirmed prices are fixed in the Mini App API.
- Payment requests store the screenshot data in PostgreSQL. Monitor database storage; optimize screenshots before upload.
