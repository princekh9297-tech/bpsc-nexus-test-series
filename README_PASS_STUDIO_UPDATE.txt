BPSC NEXUS — PREMIUM PASS STUDIO & COUPON SYSTEM

FILES
- miniapp.py: backend pass catalogue, coupon validation, payment request snapshots, admin APIs.
- static/index.html: main Mini App (recommended deployment path).
- index.html: same frontend at repository root as a fallback.
- static/membership.html: standalone membership page updated to use the dynamic catalogue and coupon validation.
- static/phonepe_qr.jpg: existing UPI QR asset; preserve it.

DEPLOY
1. Back up your current files.
2. Replace miniapp.py in the project root.
3. Replace static/index.html and static/membership.html.
4. If the project uses a root index.html fallback, replace that too.
5. Preserve static/phonepe_qr.jpg.
6. Redeploy on Render. Existing PostgreSQL tables are extended additively on startup; existing question and quiz data is not intentionally deleted.

ADMIN
Open Admin Console > Passes & Coupons.
- Create/edit passes with unique code, title, description, price, validity, access scope, entitlement keys, category, featured flag, ordering, and active status.
- Active offers appear in Get Your Pass. Deactivated offers are hidden from new purchases.
- Create/edit coupons with percent/fixed discount, cap, minimum order, start/end, redemption limit, per-user limit, applicable product codes, and active status.
- Coupon validation and final discount are recalculated server-side on payment submission. Payment request records snapshot price, discount, coupon and pass entitlement details.

PAYMENT MODEL
This update keeps the existing manual PhonePe/UPI QR + UTR + screenshot + admin approval model. It does not connect a payment gateway or automatically verify UPI transactions. Admin must verify payment in the bank/UPI app before approving a request.

TESTING
Python compilation and inline JavaScript syntax checks passed in the build environment. Live Render/PostgreSQL deployment, coupon concurrency under real traffic, Telegram group-admin checks, and end-to-end payment approval still need staging verification.
