# BPSC Nexus Mini App — V6 UI update

## Updated
- Reworked the home screen around a short personalised greeting, the existing Study Planner, and a Battle Arena card marked **Coming Soon**.
- Preserved the five navigation tabs: Home, Test Series, Create, Analytics, Profile.
- Added the five agreed themes: Obsidian Gold (default), Emerald Reserve, Midnight Cobalt, Amethyst Noir, Ivory Prestige.
- Applied the Modern Luxury typography system: Manrope for headings/navigation/buttons/scores, Inter for general UI, and Noto Sans Devanagari for Hindi.
- Added the provided BPSC Nexus logo to the splash screen and top brand lockup, keeping its aspect ratio intact.
- Retained the short, skippable branded splash.
- Refined borders, surfaces, and spacing to reduce heavy gradients, shadows, and excessive pill shapes.
- Kept planner, test series, quiz builder, analytics, and profile functionality from V5.
- Battle Arena backend/API implementation is preserved as-is. Arena API calls are not wired to the V6 UI; Arena is informational only and displays Coming Soon.
- Updated the Mini App theme preference handler to persist the five current theme choices while mapping legacy `graphite` preferences to `obsidian`. No database reset or destructive migration was added.

## Deployment
Use the existing separate Render Mini App service (`python miniapp.py`) and the same `DATABASE_URL` and `BOT_TOKEN`. Replace the repository files with the package contents, preserving your existing environment variables. Do not run any database reset or drop-table command.
