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
- `question_bank.py` — subject/collection types, collection-aware imports, bulk merge-and-delete, individual/bulk archive, topic archiving, and safe cleanup tools.
- `miniapp.py` — returns separate active Subject and Question Collection lists; archived items are excluded from student question selectors.
- `static/index.html` — separate **Subjects** and **Question Collections** tabs; collection cards open the Create Test section with that collection selected, where its set/topic filters appear.
- `static/membership.html` — membership page and current plan catalogue.
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

## Subject Library / Eduteria 7500 migration

- On startup, the question bank applies additive columns `subject_type` (`subject` / `collection`) and `archived_at`; no database reset is used.
- In Telegram Admin → Question Bank → **Create Collection**, create new multi-set bundles as collections. Existing items can be toggled between Subject and Collection in **Manage / Merge Subjects**.
- For an existing Eduteria bundle whose Set 1…Set 150 records are separate subjects: mark `Eduteria 7500` as a **Question Collection**, choose **Bulk merge into one collection/subject**, select Eduteria as the destination, then send the exact Set names (one per line or comma-separated). Review the preview and confirm. Source subjects are deleted only after their questions and text-based references are reassigned; missing topic labels become the original source names.
- **Bulk archive** and per-item **Archive** hide items from student/import selectors without deleting questions or historical attempts. Topic management can archive one or more topic names within a selected subject; matching questions become archived, but attempt snapshots remain.
- To clean unused empty subject rows, use **Delete empty subjects**; it now excludes subjects referenced by question rows, quizzes, saved attempt snapshots, or existing test series.
- New imports never create subject rows from embedded labels. For collections, unrecognized embedded subject/set labels are retained as topics under the selected collection.

## Validation performed

Python syntax checks passed for `bot.py`, `group_quiz.py`, `miniapp.py`, and `question_bank.py`. The Mini App inline JavaScript passed `node --check`. No live PostgreSQL/Telegram/Render integration test was possible in this workspace; back up the repository/database and test the merge preview on a small group of sets before bulk merging all 150.

## Recommended cleanup for an existing 150-set collection

1. Open Telegram Admin → Question Bank → Manage / Merge Subjects.
2. Find `Eduteria 7500` and tap **Make Collection** if it is currently a Subject.
3. Tap **Bulk merge into one collection/subject** and select `Eduteria 7500` as the destination.
4. Send the exact source names (for example `Set 1` through `Set 150`, one name per line). Review the source list and question count.
5. Confirm **Merge and delete sources**. For a collection destination, each source subject name becomes the question topic when a question had no topic already. Questions are reassigned and source subject rows are deleted after their text-based references are updated.
6. Open the Mini App → Study Library → **Question Collections**. Eduteria should appear as one collection; open it to go to Create Test and choose a set/topic.
7. Use **Manage topics in a subject** to archive unwanted topics. Use individual/bulk archive for clutter that should be hidden but preserved. Bulk merge is the recommended operation for old Set 1…150 subject records because it preserves their questions and set labels.

Back up PostgreSQL before a large merge. The UI previews exact matched source names and counts before the transaction is committed.
