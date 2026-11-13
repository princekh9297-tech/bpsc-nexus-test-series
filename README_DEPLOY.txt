BPSC Nexus — Plum Indigo dark appearance + subject suspension controls

Files in this patch:
- static/index.html: Plum Indigo dark theme with the same restrained border treatment used by Light Mode; removed the recently added colourful Mithila painting strips/corner overlays.
- miniapp.py: admin subject/bank suspend and activate endpoints, student-side filtering, and direct-access protection.

Deployment:
1. Replace static/index.html in the repository.
2. Replace miniapp.py in the repository.
3. Commit and push; allow Render to redeploy.

The backend adds the is_suspended column using its existing schema initialisation. No separate SQL migration is included. Verify Neon connectivity and test Suspend/Activate from an admin account after deployment.
