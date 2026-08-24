# SXO Store Bot

Telegram digital-product store bot with a menu-driven admin/staff panel.

## Features
- User menu: products, orders, coupon, referrals placeholder, support, language placeholder
- Admin menu: dashboard, products, orders, users, payment gateway, coupons, staff, broadcast, settings, tickets
- Staff roles stored in SQLite
- Product catalog + price + description
- Manual payment workflow
- Razorpay Payment Links integration
- Razorpay webhook endpoint for automatic paid status
- Admin can add a product and then upload its APK/ZIP/PDF/document; Telegram `file_id` is saved for delivery
- FastAPI health endpoint for cloud hosting
- Polling locally; webhook automatically when PUBLIC_URL is set

## Important
The bot does NOT include gambling, betting, or prediction features.

## Android-first setup
1. Install Termux from a trusted source.
2. Install Python and Git.
3. Put this project in a folder.
4. Run:
   `pip install -r requirements.txt`
5. Create `.env` from `.env.example` and set BOT_TOKEN and ADMIN_IDS.
6. For local testing, leave PUBLIC_URL empty and run `python bot.py`.
7. For 24/7 production, deploy to a cloud service and use a persistent Postgres database for important stores/orders.

## Admin
Send `/start` from an account whose Telegram numeric ID is in ADMIN_IDS.
Everything is controlled through Telegram inline menus.

## Product files
For production, add a small admin workflow to save Telegram file_id for each product. Telegram file IDs are better than repeatedly uploading files. This starter already delivers a file when `products.file_id` is populated.

## Razorpay
Admin -> Payment Gateway -> Razorpay -> send:
`KEY_ID | KEY_SECRET`

Configure Razorpay webhook to:
`https://YOUR_DOMAIN/razorpay/webhook`
with the same RAZORPAY_WEBHOOK_SECRET.

Use only credentials belonging to your own verified merchant account and follow the gateway's terms/KYC requirements.

## Hosting
For testing, Railway provides a one-time trial credit and then limited free credit; see current pricing before deploying.
Render free services are intended for testing/hobby and have limitations.
Koyeb has a free web instance, but it can scale to zero and is not suitable as the sole production persistence layer.

For a serious 24/7 store, use a small paid service + managed Postgres.
