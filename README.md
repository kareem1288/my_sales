# My Sales — Store Management (FastAPI)

My Sales is a retail billing app: POS with barcode lookup, Cash/UPI/Card/split payments, printable thermal receipts, a sales dashboard, master data (items, groups, customers, tax templates, companies, users), reports, self-service store signup with a 20-day trial, OTP login, and the MSA assistant.

This is a standalone rewrite of the original Frappe/ERPNext app. It runs on **FastAPI + SQLAlchemy** and needs no Frappe bench, ERPNext, Redis or background workers. You can run it locally with SQLite, or deploy it to Vercel (or any Python host) with Postgres.

## Quick start (local)

```bash
uv venv -p 3.12 .venv            # or: python3.12 -m venv .venv
uv pip install -p .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m app.cli seed-demo          # demo store with 60 days of bills
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 and sign in with `demo@example.com` / `Demo@1234`.

Run the tests with `.venv/bin/python -m pytest`.

## Command-line tasks

| Command | Purpose |
| --- | --- |
| `python -m app.cli init-db` | Create the database tables. The app also does this on startup. |
| `python -m app.cli create-admin EMAIL PASSWORD` | Create a system administrator who can see every store. |
| `python -m app.cli create-store "Store" EMAIL PASSWORD` | Create a store and its manager without using the signup page. |
| `python -m app.cli seed-demo [--days N]` | Create the demo store. Safe to run more than once. |
| `python -m app.cli expire-trials` | Disable users whose trial has ended. On Vercel, a daily cron does this instead. |

## Deploy to Vercel

1. Create a Postgres database, for example Neon from the Vercel Marketplace, and copy its connection string.
2. Import this repository in Vercel. `pyproject.toml` already sets the entrypoint (`[tool.vercel] entrypoint = "index:app"`, which loads `app.main`).
3. Add these environment variables:
   - `DATABASE_URL`: the Postgres URL. Both `postgres://` and `postgresql://` work.
   - `SECRET_KEY`: a long random string. It signs session cookies.
   - `CRON_SECRET`: a random string. Vercel sends it to the daily trial-expiry cron in `vercel.json`.
   - `SITE_URL`: your public URL, used in password-reset emails.
   - Optional email, SMS, UPI and assistant settings. See [.env.example](.env.example).
4. Deploy. The tables are created on first start. To add an administrator, run `create-admin` once from your machine with `DATABASE_URL` pointing at the production database.

The app refuses to start on Vercel with SQLite, because Vercel's filesystem is temporary and every bill would be lost.

## Configuration

All settings are environment variables. See [.env.example](.env.example) for the full list. Settings that used to live in Frappe settings documents are now environment variables:

| Was (Frappe) | Now |
| --- | --- |
| UPI Configuration | `UPI_ENABLED`, `UPI_MODE` (`Testing` or `Live`), `UPI_ID`, `UPI_PAYMENT_API_URL`, `UPI_API_KEY`, `UPI_API_SECRET` |
| MSA Settings | `MSA_ENABLED`, `OLLAMA_URL`, `OLLAMA_MODEL` |
| Brevo OTP Settings | `BREVO_API_KEY`, `BREVO_SMS_SENDER` |
| Email Account | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` |

## How it maps to the original app

- **Same URLs and API.** Pages are still served at `/pos`, `/masters`, `/reports`, `/assistant`, `/more`, `/store-login` and `/store-signup`. API methods keep their Frappe names, for example `/api/method/store_management.api.create_pos_bill`. The existing HTML, CSS and JS are reused almost unchanged. [app/static/js/frappe-shim.js](app/static/js/frappe-shim.js) provides the small part of the Frappe browser API the pages use: `frappe.call`, `msgprint`, `show_alert`, `FileUploader` and `__()`.
- **Each store's data is isolated.** Every item, customer, bill and report setting belongs to one company. A user only sees the stores they belong to. Signup creates the store, its manager, default groups, a Walk-in Customer and the report sidebar.
- **Roles.** Users get role profiles: `Store Manager` (everything), `Cashier` (billing, reports, adding customers) or `Accountant`.
- **Bills.** Each POS sale creates a submitted bill plus one payment row per payment method, numbered `SINV-YYYY-#####` and `PAY-YYYY-#####`. Stock is reduced for stock items, and totals are rounded to the nearest rupee as ERPNext did. Receipts render at `/printview?name=<bill>` from the original thermal template. Use the browser's print dialog to print or save a PDF.
- **Reports.** These are rebuilt from bills, payments and expenses: Sales Register, Item-wise Sales, Customer Ledger, Accounts Receivable, Profit & Loss, General Ledger and Trial Balance. You can also add Payment Summary, Daily Sales Summary and Stock Balance from *Manage Reports*. The accounting reports use a small fixed chart of accounts instead of a full ERPNext ledger.
- **Expenses.** Expenses replace ERPNext Purchase Invoices in the dashboard and P&L. Record them under *Masters → Expenses*, and view them in the *Expenses* report linked from the sidebar.
- **Uploads.** Uploaded files are stored in the database, so they work on serverless hosts. Only images are accepted, up to 5 MB. Files are visible only to users of the same store.

## Not carried over from ERPNext

- **Taxes.** Item Tax Templates can be managed, but they are not yet applied to bills.
- **Receipt PDFs.** Bills no longer get a WeasyPrint PDF attached; print or save the receipt from the browser instead.
- **Frappe Insights.** Insights dashboards are not available.
- **Android app.** The wrapper in the original repository points at `mysales.m.frappe.cloud`. Update its URL, and `ANDROID_UPLOAD_CERT_SHA256` in `app/main.py`, when you move to a new domain.

## Project layout

```
app/
  main.py          FastAPI app: pages, /api/method dispatcher, uploads, receipts, PWA, cron
  models.py        Database tables
  auth.py          Passwords (scrypt), sessions, roles, rate limiting
  setup.py         Reference data and per-store defaults
  services/        pos, dashboard, masters, reports, accounts (signup/login/OTP), assistant, notify
  templates/       Page templates (from the original www/), receipt, base layout
  static/          Original CSS/JS plus frappe-shim.js
  cli.py           Admin and demo-data commands
tests/             End-to-end API tests
```
