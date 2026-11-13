# BPSC Nexus latest frontend update

Updated the existing `static/index.html` from the latest integrated project archive. No other project files were changed.

- Global appearance now has two options: existing Light appearance and Dark AMOLED.
- Appearance selection is stored only in browser localStorage; no Neon preference API call and no SQL migration.
- Removed subject-wise quiz theme selection and per-quiz theme application.
- Removed subject Trash/Restore controls and added a permanent-delete action. The existing backend endpoint performs archive-then-purge internally through two API calls; successful deletion removes the temporary trash records. If quiz/attempt/history/test-series references exist, deletion is blocked and the subject is restored when possible.

Deploy by replacing `bpsc-nexus-test-series-main/static/index.html` in the repository.
