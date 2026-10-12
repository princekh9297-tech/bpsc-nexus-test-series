# V9 — Avatar picker and compact mascot emotes

- Added 10 selectable profile avatars: Classic, Royal, Minimal, Stealth, Scholar, Warrior, Chibi, Neon, Monochrome and Nexus.
- Avatar selection is stored locally on the user's device and updates the Home portrait and Profile preview.
- Added 28 individually optimized WebP emote assets for use in future interface states.
- Added a small success emote to the submitted quiz result screen only; no mascot overlays appear during active questions or timers.
- Telegram `/start` and `/tutorial` now use a compact hello emote instead of the full-size mascot image. User-facing text does not repeatedly name the mascot.
- No database schema, user data, quiz scoring, CBT APIs, or Battle Arena behavior was changed.
- The selected avatar is currently device-local; account-wide sync is not implemented in this release.
