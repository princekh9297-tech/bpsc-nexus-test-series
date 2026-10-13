# BPSC Nexus — Subject, Mini App and Membership Update

This update preserves the existing PostgreSQL database and keeps the PhonePe/UPI manual-payment workflow. It adds the requested subject/topic work from the previous package and updates the membership flow to the new plans.

## New premium plans

- **Group Starter — ₹99 / 30 days**: activates group-quiz access for one selected Telegram group only.
- **Group Elite — ₹249 / lifetime**: lifetime group-quiz access for one selected Telegram group only.
- **Individual Pro — ₹79 / 60 days**: personal All Access.
- **Individual Prime — ₹149 / lifetime**: personal All Access.
- Existing **Individual Question Banks** remain a separate category. Approval grants only the selected bank entitlement, never All Access.
- Coaching institute test series announcement: **₹49 per series — Coming Soon** (announcement only; not purchasable yet).

## Files in this package

- `bot.py` — new plan announcements, `/groupid`, payment review/activation routing for group, individual and individual-question-bank products.
- `miniapp.py` — serves the plan catalogue, validates group-admin status for group purchases, records target group IDs and payment requests.
- `group_quiz.py` — additive group membership expiry/plan columns and expiry-aware access checks.
- `question_bank.py` — subject/topic import and duplicate-management changes from the previous request.
- `static/index.html` — Mini App UI changes from the previous request.
- `static/membership.html` — new membership page with three clear categories, plan cards, group ID helper, PhonePe/UTR/screenshot steps, payment history and Coming Soon announcement.
- `README.md`, `requirements.txt`, `render.yaml`, `render-miniapp.yaml`, `schema.sql`, `leaderboard.py` — deployment/context files included for reference; don't replace an existing file unless you intend to update it.

## GitHub deployment

1. Back up the repository before replacing files.
2. Replace only these files in the existing repository: `bot.py`, `group_quiz.py`, `miniapp.py`, `question_bank.py`, `static/index.html`, and `static/membership.html`.
3. Keep existing static assets, especially `static/phonepe_qr.jpg`, and any other files that are not in this package. Do not delete the rest of the `static/` folder.
4. Commit and push to GitHub.
5. Redeploy both the Telegram bot service and Mini App service on Render. Both services must continue using the same existing `DATABASE_URL`; do not reset the database.
6. In the target Telegram group, add the bot and run `/groupid`. The purchaser must submit the Group Pass from the same Telegram account that is an administrator of that group. The Mini App validates this through Telegram's `getChatMember` API, so the bot must be present and have enough group permissions for Telegram to report admin status.
7. Open the Membership page, select a pass, pay the exact amount through PhonePe, enter the UTR and upload the screenshot. Access is activated only after manual admin approval in the bot's Payment Requests panel.

## Safety and migration notes

- Schema changes are additive (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`); no table/database reset is performed.
- Existing `group_quiz_memberships` rows with no expiry remain active as before; new paid Starter rows expire after 30 days, while Elite rows have no expiry.
- New group purchases apply only to the submitted group ID. No global/all-groups grant is performed.
- Individual Pro/Prime grants personal `all_access`; a Question Bank purchase grants only its selected vault key.
- Old `all_access` purchase submissions are no longer accepted by the updated Mini App API, but already-pending legacy requests can still be reviewed in the bot.
- Test with one group Starter, one individual Pro and one individual Question Bank request before announcing the plans. Syntax checks passed, but a live Render/Telegram/PostgreSQL deployment was not available to verify end-to-end behavior here.
