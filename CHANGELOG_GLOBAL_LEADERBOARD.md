# Global Leaderboard update

- Added shared verified-attempt ledger with unique source keys and incremental weekly/monthly/lifetime/subject summaries.
- Integrated completed Mini App CBT and Telegram question-bank attempts; integrated participant-level scores from completed Telegram group quizzes.
- Added historical one-time backfill for completed attempts that contain reliable stored scores/answer keys.
- Added Weekly, Monthly, Daily Challenge, Subject-wise and Lifetime ranking API/UI, persistent My Rank, UTC period selection and ranking policy disclosure.
- Added admin daily-challenge configuration, participation counts, flagged-attempt review and audited exclusions with summary rebuild.
- Preserved the existing Mini App design system and Battle Arena's current “Coming Soon” panel/behavior.
- Added only additive, idempotent schema changes; no existing table is dropped or reset.
