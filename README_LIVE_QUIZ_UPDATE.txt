BPSC NEXUS — LIVE QUIZ CONTROL + RESUME UPDATE

FILES
- miniapp.py: PostgreSQL-backed sessions, participant registration, live presence, shared timer, admin start/pause/resume/end controls, and attempt linking.
- static/index.html: recommended Mini App frontend deployment path.
- index.html: matching root fallback copy.

STUDENT FEATURES
- Dedicated Live tab and Home shortcut.
- Browse upcoming, open, live, paused, and recently ended sessions.
- See total joins and users active in the last 90 seconds.
- Join a live quiz, close the Mini App, and return later while the session is still running.
- Existing attempt and answers are stored server-side; returning users resume the same attempt instead of creating a new one.
- The shared session clock is controlled by the server. Closing the app does not stop it.
- If the administrator pauses the session, the shared clock freezes and answer submissions are blocked until it resumes.
- The session automatically changes to Ended when its shared duration expires (checked by active session-state requests).

ADMIN FEATURES
- Admin Console > Live Quizzes.
- Create sessions from published quizzes and set duration/schedule.
- Start / Resume, Pause, and End buttons.
- View total participants, active users (heartbeat in the last 90 seconds), and remaining shared time.
- Status dropdown remains available for scheduling/opening/cancelling sessions.

DEPLOY
1. Back up the current project files.
2. Replace miniapp.py in the project root.
3. Replace static/index.html. The root index.html is included as a matching fallback.
4. Keep existing environment variables and other files unchanged.
5. Redeploy the Render service and test with an admin plus two student accounts.
6. Startup performs additive PostgreSQL migrations for live-session timing/presence fields. No question bank or existing attempt records are intentionally deleted.

SCOPE / LIMITS
- This implements a shared session clock and persistent per-student attempts. Each student's current question position remains their own; this is not a host-controlled broadcast where every participant is forced to see the exact same question at the same moment.
- Active-user count means a participant sent a heartbeat in the last 90 seconds; it is an approximate online count, not a count of everyone who has ever joined.
- Database integration and end-to-end behavior must still be verified against the deployed PostgreSQL/Render environment; syntax checks alone cannot guarantee production behavior.

VALIDATION PERFORMED
- Python syntax compilation passed.
- Inline JavaScript syntax check passed.
- Live database and Telegram Mini App end-to-end test not available in this workspace.
