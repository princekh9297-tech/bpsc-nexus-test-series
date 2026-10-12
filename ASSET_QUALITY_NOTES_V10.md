# V10 Asset Quality Fixes

- Fixed avatar loading by publishing flat `/static/avatar-*.webp` paths compatible with the current one-segment static asset route.
- Avatar images now use `object-fit: contain` and a consistent premium navy/gold background to avoid clipping the art.
- Removed the sheet-derived emote from quiz results; replaced it with a clean native success mark.
- Telegram bot no longer sends sheet-cropped emote images; it uses clean text until individually art-directed transparent assets are ready.
- The original `static/avatars/` and `static/emotes/` folders are retained as source/reference only and are not used by the UI.
- No quiz logic, database schema, database data, or Battle Arena behavior was intentionally changed.

## Verification needed after deployment
Open `/static/avatar-classic.webp` directly, then check Profile avatar picker and Home avatar in Telegram WebView. Clear Telegram WebView cache/reopen Mini App if stale files persist.
