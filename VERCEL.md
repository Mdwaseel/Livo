# Deploying Livo OS to Vercel

Vercel deploys Livo OS with its built-in Django support. It finds
`manage.py`, reads `WSGI_APPLICATION` (`config.wsgi.application`) from the
settings, runs `collectstatic` during the build, and serves `/static/` from its
CDN. Everything else runs in one Python function. `vercel.json` only raises
that function's time limit to 60 seconds.

The VPS setup in `DEPLOYMENT.md` still works unchanged. Everything
Vercel-specific in `config/settings.py` switches on only when Vercel's `VERCEL`
environment variable is present.

## Demo deploy (no database to set up)

Without `DATABASE_URL`, the app runs as a self-contained demo on
`demo/livo_demo.sqlite3`, which is uploaded with the code.

- Each function instance copies that file to `/tmp` when it starts.
  Everything works, including creating clients, projects and documents, but
  changes belong to that instance and reset whenever Vercel recycles it. That
  can happen after a few minutes of inactivity.
- Sessions are kept in signed cookies, so staying signed in does not depend on
  which instance serves a request.
- The demo database starts empty apart from the document types and one owner
  account. It is built clean, never copied from your local `db.sqlite3`.

### 1. Build the demo database (already done; rebuild after model changes)

```bash
python manage.py build_demo_db --password 'Livo@123#'   # owner: hsn
```

Rebuild it whenever a new migration is added. Otherwise the demo starts on an
out-of-date schema.

### 2. Set one environment variable

In Vercel, under Project → Settings → Environment Variables:

| Variable | Value |
|---|---|
| `SECRET_KEY` | **Required, for every environment (Production and Preview).** A long random string: `python -c "import secrets; print(secrets.token_urlsafe(50))"`. The build loads the settings, so a missing key fails the build with `SECRET_KEY is unset`. The key also signs the session cookies, so never reuse a public value. |
| `MAX_UPLOAD_MB` | Optional. Set it to `4`, because Vercel rejects request bodies over 4.5 MB. |
| `GROQ_API_KEYS` | Optional. Enables AI drafting; without it the app returns placeholder drafts. |
| `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | Only for a custom domain, e.g. `os.livodigital.com` and `https://os.livodigital.com`. The `*.vercel.app` hostnames are added automatically. |

Leave `DEBUG` unset, because it defaults to off on Vercel. If it is off and
`SECRET_KEY` is missing, the app refuses to start rather than run with the
development key.

### 3. Deploy

Either import the repository in the Vercel dashboard (Vercel detects
**Django** on its own, so leave the build settings empty), or use the CLI from
the project folder:

```bash
npm i -g vercel
vercel          # first run links the project and makes a preview deploy
vercel --prod
```

Sign in as `hsn`. The six-digit code appears on the verification screen.

After adding or changing an environment variable, redeploy. Variables only
reach new builds.

`.vercelignore` keeps `.env`, your local `db.sqlite3`, `media/`, `.venv/` and
other local files out of the upload. `demo/livo_demo.sqlite3` must be
committed, because the demo runs on it.

## Going beyond a demo: persistent data

Set `DATABASE_URL` to a Postgres URL, for example Neon's **pooled** connection
string with `?sslmode=require`. Demo mode then switches off. Before deploying,
run the setup once from your machine against that database:

```bash
export DATABASE_URL="postgresql://...-pooler.../neondb?sslmode=require"
python manage.py migrate
python manage.py seed_doctypes
python manage.py ensure_owner hsn --password '...'
```

## Limits on Vercel, in either mode

- **Uploads do not persist.** Uploads are written to `/tmp`: that space belongs
  to one instance, is wiped when the instance restarts, and is not served back
  to browsers. For uploads that last, the storage backend needs to point at
  object storage, such as S3, Cloudflare R2 or Vercel Blob.
- **No scheduled jobs.** `send_calendar_reminders`, `send_client_updates` and
  `generate_recurring_tasks` ran from cron on the VPS. Nothing runs them on
  Vercel.
- **PDF engine.** WeasyPrint needs system libraries that Vercel does not have,
  so PDFs use the built-in xhtml2pdf fallback, which the app detects
  automatically.
- **Request limits.** Each request can run for up to 60 seconds and send at
  most 4.5 MB.
- **Emails.** Password reset and notifications need the `EMAIL_*` variables.
  Sign-in never sends email.
