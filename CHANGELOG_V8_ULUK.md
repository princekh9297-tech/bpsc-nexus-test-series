# V8 — ULUK Mascot Integration

- Added the supplied original ULUK mascot image as `static/uluk.png`.
- Replaced the splash image with the original mascot while preserving the existing splash duration and skip control.
- Added a compact, static ULUK portrait beside the Home greeting only; no mascot is displayed over active quiz screens.
- Updated `/start` and `/tutorial` to send the mascot image with the existing Mini App keyboard. If the image cannot be sent, the bot safely falls back to text.
- No database schema, user data, quiz scoring, CBT API, or Battle Arena behavior was changed. Battle Arena remains as currently implemented.
