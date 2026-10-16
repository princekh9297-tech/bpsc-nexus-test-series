BPSC Nexus Mini App — update bundle

Overlay these files into your EXISTING Mini App repository; do not delete other files.
- miniapp.py: updated backend with server-side demo gate and identity-verification audit/rate limiting.
- index.html and static/index.html: matching updated frontend. The server serves static/index.html when that path exists, so both copies are included intentionally.
- Keep your existing static/membership.html, static images, bot files, environment variables, and other modules unchanged.

Deploy the Mini App service after copying these files. Test on a staging service first: this bundle was syntax/static-checked but not tested against your live Telegram bot or PostgreSQL database.
