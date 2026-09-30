# Livo OS

Internal AI document-automation console for Livo Digital.
**Client → Project → Documents.** Pick a document type, fill a short brief, AI drafts it,
edit in the rich-text editor, save with version history, export to PDF.

## Stack
Django 5.2 · PostgreSQL (SQLite for local dev) · WhiteNoise · Quill editor ·
PDF export via WeasyPrint with pure-Python xhtml2pdf fallback (auto-detected) ·
Anthropic/OpenAI for generation.

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # optional: add an AI key
python manage.py migrate
python manage.py seed_doctypes  # loads the standard agency document types
python manage.py createsuperuser
python manage.py runserver
```
Open http://127.0.0.1:8000 — sign in with the superuser.

## Deploy
- **Vercel:** see `VERCEL.md` (runs as a self-contained demo with no database; add Postgres for persistent data).
- **VPS (nginx + gunicorn):** see `DEPLOYMENT.md`.

## Login
The local `db.sqlite3` ships with a superuser named `admin`. Its password is **not**
recorded here on purpose — a credential in a tracked file is a credential in every
copy of the repo. Ask the account owner, or set your own:

```bash
python manage.py changepassword admin
```

Sign-in is two-step: password, then a six-digit code shown on the verification
screen. The code is not emailed, so this step does not add real second-factor
security — it only keeps the flow's shape. `OTP_LOGIN_REQUIRED=False` in `.env`
skips the step entirely. Password reset still sends an email, so it needs SMTP.

Without an AI key the generator returns a placeholder draft so the full flow is testable.

## Architecture note
There is **one document engine**, not one app per document type. Every document type
(Proposal, Contract, Invoice…) is a `DocumentType` row with its own form fields + AI prompt
+ template. Add a new document type in admin — no code.

See `ROADMAP.md` for the build plan.
