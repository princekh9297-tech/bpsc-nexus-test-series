# BPSC Nexus Mini App — V7 Refinement Pass

## Premium polish
- Standardised card geometry, spacing, typography hierarchy, touch targets, form controls, navigation, and focus states.
- Improved bilingual question readability and small-screen exam controls.
- Reduced unnecessary visual effects and added reduced-motion support.
- Improved live-region accessibility for errors and planner task updates.
- Moved the font import to the start of the stylesheet so browsers can load it reliably.

## Telegram bot access policy
- Public user commands are limited to `/start`, `/mypremium`, `/tutorial`, and `/help`.
- Published quiz attempts, quiz browsing, and group quiz polling are no longer launched from Telegram chat; students should use the Mini App.
- `/admin` and admin-only question-bank management remain available to configured administrators for maintenance because the current Mini App does not replace those admin management screens.
- Existing database tables and question data are preserved. No destructive migration is included.
- Battle Arena remains Coming Soon in the frontend. Arena backend code and routes are left untouched.

## Validation
- Python modules pass `py_compile`.
- Inline JavaScript syntax and ZIP integrity are checked during packaging.
- Live Render deployment and database integration are not verified by this local package build.
