# Deploying Livo OS to the BigRock VPS

Target: **https://za-an.com** (and `www.za-an.com`)
Server: BigRock VPS Linux KVM (India), NVMe 4, order `125811606`
Database: **PostgreSQL**
Code delivery: **private GitHub repo → `git pull` on the server**

> **The VPS already serves 4strokeinternational.com.** Every step below is written to
> leave that site untouched. The two rules that matter: never replace the web server
> that already owns ports 80/443, and never edit that site's existing config file. We
> only ever *add* a new server block.

---

## What you are building

```
                    ┌─────────────────────────────────────────────┐
  Browser ──443──►  │ nginx  (already running, already owns :443) │
                    │                                             │
                    │  4strokeinternational.com → existing site   │  ← untouched
                    │                                             │
                    │  za-an.com                                  │
                    │    /static/    → files on disk (fast path)  │
                    │    /protected/ → internal only, never public│
                    │    /           → proxy to gunicorn          │
                    └──────────────────┬──────────────────────────┘
                                       │ unix socket
                    ┌──────────────────▼──────────────────────────┐
                    │ gunicorn (systemd service, 3 workers)       │
                    │   config.wsgi:application                   │
                    └──────────────────┬──────────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────────┐
                    │ PostgreSQL 15/16  ·  database livo_os│
                    └─────────────────────────────────────────────┘
```

**Why `/protected/` exists.** Uploaded files live under `media/`, and their names are
guessable — `media/generated/inv-2026-001-v1.pdf` is a real invoice. Employee CVs and
ID proofs sit in `media/employee_docs/`. If nginx served `/media/` directly, anyone
could download all of it without logging in. Instead nginx marks that location
`internal`, Django checks the session, and only then hands the file back to nginx to
stream. Step 1.3 adds the code that does this.

---

## Collect these before you start

| Thing | Where to find it | Value |
|---|---|---|
| VPS IPv4 address | BigRock panel → VPS → Manage | `___.___.___.___` |
| Root (or sudo) SSH access | BigRock welcome email | |
| GoDaddy login | for the `za-an.com` DNS records | |
| GitHub account | to create the private repo | |

Two credentials are already configured in your local `.env` and must be carried over
by hand (they are deliberately **not** in git): the Gmail app password for
`dev.digitalvint@gmail.com`, and the Groq API key.

---

## Step 0 — Survey the server first

SSH in and find out exactly what you are dealing with. **Do not skip this.**

```bash
ssh root@YOUR_VPS_IP

cat /etc/os-release                       # Ubuntu/Debian or AlmaLinux/Rocky/CentOS?
python3 --version                         # Django 5.2 needs 3.10 or newer
nginx -v 2>&1 || httpd -v 2>&1            # which web server is installed?
ss -tlnp | grep -E ':80|:443'             # which process actually owns the ports?
systemctl list-units --type=service --state=running | grep -Ei 'nginx|httpd|apache|postgres|mysql'
df -h /                                   # free disk
free -m                                   # free RAM
nproc                                     # CPU count — sets the worker count later
```

Write down the answers. Three of them change later steps:

- **Apache instead of nginx?** Jump to [Appendix A](#appendix-a--if-apache-owns-the-ports-instead-of-nginx).
- **Python older than 3.10?** See [Appendix B](#appendix-b--installing-a-newer-python).
- **AlmaLinux / Rocky / CentOS?** SELinux is enforcing; see [Appendix C](#appendix-c--selinux-rhel-family-only).

Back up the existing site's config before you touch anything in that directory:

```bash
mkdir -p /root/preflight-backup
cp -a /etc/nginx /root/preflight-backup/nginx-$(date +%F)
nginx -T > /root/preflight-backup/nginx-full-config-$(date +%F).txt
```

---

## Step 1 — Five changes to make locally, before the first push

The app is production-ready except for these. Make them on Windows, then push.

### 1.1 — Add the production dependencies

Edit `requirements.txt`:

```
Django>=5.2,<6.0
dj-database-url>=2.1
python-dotenv>=1.0
Pillow>=10
xhtml2pdf>=0.2.16

# --- production ---
gunicorn>=22.0
psycopg[binary]>=3.2
# Better PDF output than xhtml2pdf; works on Linux, skipped automatically if the
# native libs are missing. Install the system packages in Step 3.
weasyprint>=62
```

> `psycopg2-binary` is deliberately not used — it has no wheels for the Python 3.14 you
> run locally. `psycopg` (version 3) is supported natively by Django 5.2 and needs no
> settings change; `dj-database-url` reads the same `postgres://` URL either way.

### 1.2 — Add one setting

At the end of `config/settings.py`, in the transport-hardening block:

```python
# nginx can stream files far more efficiently than Python can. When this is on,
# the media view returns an X-Accel-Redirect header instead of the bytes, and
# nginx serves the file from its `internal` location. Off in dev, where Django
# serves media itself.
USE_X_ACCEL_REDIRECT = _flag("USE_X_ACCEL_REDIRECT", False)
```

### 1.3 — Add the protected media view

Create **`core/views_media.py`**:

```python
"""Uploaded files are private, and their names are guessable.

`media/generated/inv-2026-001-v1.pdf` is a real invoice; `media/employee_docs/`
holds CVs and ID proofs. Serving MEDIA_ROOT straight from nginx would hand all of
it to anyone who could guess a filename, so in production nothing reaches the
filesystem without passing through here first.

nginx declares `location /protected/ { internal; }`, which browsers cannot request
directly — only an X-Accel-Redirect header from this app can reach it. So the
session check below is not bypassable.
"""
import os
import posixpath
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse
from django.views.decorators.http import require_safe


def _safe_relative_path(path):
    """Refuse anything that tries to climb out of MEDIA_ROOT."""
    clean = posixpath.normpath("/" + path.replace("\\", "/")).lstrip("/")
    if not clean or ".." in clean.split("/"):
        raise Http404
    return clean


@require_safe
@login_required
def serve_media(request, path):
    rel = _safe_relative_path(path)
    full = os.path.join(settings.MEDIA_ROOT, *rel.split("/"))
    if not os.path.isfile(full):
        raise Http404

    if getattr(settings, "USE_X_ACCEL_REDIRECT", False):
        response = HttpResponse()
        # Hand the job back to nginx: it streams the bytes, we only decided
        # whether the caller was allowed to have them.
        del response["Content-Type"]        # let nginx pick it from the extension
        response["X-Accel-Redirect"] = "/protected/" + quote(rel)
        return response

    # Any server without X-Accel-Redirect support: stream it ourselves.
    return FileResponse(open(full, "rb"))
```

Then wire it up at the bottom of **`config/urls.py`**:

```python
from django.urls import include, path, re_path   # add re_path to the existing import

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.BASE_DIR / "static")
else:
    # In production nothing serves MEDIA_ROOT directly. Every uploaded file is
    # fetched through the login check in core.views_media.
    from core.views_media import serve_media
    urlpatterns += [
        re_path(r"^media/(?P<path>.*)$", serve_media, name="protected_media"),
    ]
```

This needs no template changes — `MEDIA_URL` already normalises to `/media/`, so
existing links like `{{ f.file.url }}` route through the new view automatically.

### 1.4 — Tidy `.gitignore`

```
__pycache__/
*.pyc
.env
db.sqlite3
/media/
/staticfiles/
.venv/
.coverage
htmlcov/
*.sqlite3
.claude/
```

### 1.5 — Clean the test litter out of `media/`

248 of the 269 files under `media/` are leftovers from test runs. They are not
referenced by any database row. You do not want them on the server:

```powershell
# From the project root, in PowerShell
Remove-Item media\employee_docs\cv_*.pdf
Remove-Item media\task_attachments\* -Include *_*.png,*_*.pdf
```

Do not delete `media/branding/` or anything currently referenced. If in doubt, skip
this step — it costs 500 KB, not correctness.

### 1.6 — Confirm nothing broke

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test --noinput > testlog.txt 2>&1
Select-String -Path testlog.txt -Pattern "Ran \d+ tests|^OK|FAILED"
```

Expect `Ran 942 tests` and `OK`. (Do not pipe the test run through `tail` — Django
writes the summary to stderr and it gets discarded.)

---

## Step 2 — Put the code on GitHub

The project is **not a git repository yet.** From the project root in PowerShell:

```powershell
git init
git branch -M main
git add .
git status                # ← READ THIS. Confirm .env and db.sqlite3 are NOT listed.
```

> Stop if `.env` appears in that list. It holds your Gmail app password and Groq API
> key. Fix `.gitignore` first, then `git rm --cached .env` if it was already staged.

```powershell
git commit -m "Livo OS - initial commit for deployment"
```

Now create the repo. On GitHub: **New repository → name `livo_os` → Private →
do not add a README or .gitignore** (you already have both). Then:

```powershell
git remote add origin https://github.com/YOUR_USERNAME/livo_os.git
git push -u origin main
```

### Give the server read-only access with a deploy key

On the **VPS**:

```bash
ssh-keygen -t ed25519 -C "vps-livo-os" -f /root/.ssh/id_ed25519_livo -N ""
cat /root/.ssh/id_ed25519_livo.pub
```

Copy that public key. On GitHub: **your repo → Settings → Deploy keys → Add deploy
key** → paste it → **leave "Allow write access" unchecked**. A read-only key means a
compromised server cannot rewrite your source.

Tell SSH to use it for GitHub:

```bash
cat >> /root/.ssh/config <<'EOF'

Host github.com
    HostName github.com
    User git
    IdentityFile /root/.ssh/id_ed25519_livo
    IdentitiesOnly yes
EOF
chmod 600 /root/.ssh/config
ssh -T git@github.com          # expect: "Hi USERNAME/livo_os! You've successfully authenticated"
```

---

## Step 3 — Prepare the server

### Install packages

**Ubuntu / Debian:**

```bash
apt update
apt install -y python3 python3-venv python3-dev build-essential \
               postgresql postgresql-contrib libpq-dev \
               nginx git curl \
               libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi-dev
```

**AlmaLinux / Rocky / CentOS:**

```bash
dnf install -y python3 python3-devel gcc make \
               postgresql-server postgresql-contrib libpq-devel \
               nginx git curl \
               pango cairo gdk-pixbuf2 libffi-devel
postgresql-setup --initdb
systemctl enable --now postgresql
```

The pango/cairo packages are what let **WeasyPrint** work. `documents/pdf.py` detects
it automatically and produces noticeably better PDFs than the xhtml2pdf fallback you
get on Windows. If they fail to install, the app still works — it just falls back.

### Create a dedicated user

Never run the app as root.

```bash
adduser --system --group --home /srv/livo_os --shell /bin/bash livo
# AlmaLinux/Rocky: adduser --system --home /srv/livo_os --shell /bin/bash livo
```

---

## Step 4 — PostgreSQL

Generate an **alphanumeric** password. Characters like `@ / : #` have meaning inside a
`DATABASE_URL` and would need percent-encoding — sidestep the problem entirely:

```bash
openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32; echo
```

Save that string. Then:

```bash
sudo -u postgres psql <<'EOSQL'
CREATE DATABASE livo_os ENCODING 'UTF8';
CREATE USER livo_os WITH PASSWORD 'PASTE_THE_PASSWORD_HERE';

-- Django needs these for its own connection handling and for running tests.
ALTER ROLE livo_os SET client_encoding TO 'utf8';
ALTER ROLE livo_os SET default_transaction_isolation TO 'read committed';
ALTER ROLE livo_os SET timezone TO 'Asia/Kolkata';

GRANT ALL PRIVILEGES ON DATABASE livo_os TO livo_os;
EOSQL

# PostgreSQL 15+ : the public schema is no longer writable by default.
sudo -u postgres psql -d livo_os -c \
  'GRANT ALL ON SCHEMA public TO livo_os; ALTER SCHEMA public OWNER TO livo_os;'
```

Check it:

```bash
psql "postgres://livo_os:PASSWORD@127.0.0.1:5432/livo_os" -c '\conninfo'
```

---

## Step 5 — Clone the code

```bash
mkdir -p /srv
git clone git@github.com:YOUR_USERNAME/livo_os.git /srv/livo_os
chown -R livo:livo /srv/livo_os
cd /srv/livo_os
```

---

## Step 6 — Virtualenv and dependencies

```bash
sudo -u livo python3 -m venv /srv/livo_os/.venv
sudo -u livo /srv/livo_os/.venv/bin/pip install --upgrade pip
sudo -u livo /srv/livo_os/.venv/bin/pip install -r /srv/livo_os/requirements.txt
```

Confirm WeasyPrint imported cleanly (optional but nice to know):

```bash
sudo -u livo /srv/livo_os/.venv/bin/python -c \
  "import weasyprint; print('WeasyPrint', weasyprint.__version__, 'OK')"
```

---

## Step 7 — The production `.env`

Generate a fresh secret key — **do not reuse the development one**:

```bash
/srv/livo_os/.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Create `/srv/livo_os/.env`. Note the HTTPS switches are **off** for now; they
get turned on in Step 13, after the certificate exists. Turning them on early makes it
impossible to log in, because secure cookies are never sent over plain HTTP.

```bash
sudo -u livo tee /srv/livo_os/.env > /dev/null <<'EOF'
# ===== Livo OS - production =====
SECRET_KEY=PASTE_THE_FRESH_KEY_HERE
DEBUG=False
ALLOWED_HOSTS=za-an.com,www.za-an.com
# Used to build links in emails sent by cron jobs, which have no request to take
# the host from. Without it, recurring-task emails go out with no "open" button.
SITE_URL=https://za-an.com

DATABASE_URL=postgres://livo_os:DB_PASSWORD_HERE@127.0.0.1:5432/livo_os

# --- Email (sign-in codes + password reset) ---
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=dev.digitalvint@gmail.com
EMAIL_HOST_PASSWORD=YOUR_GMAIL_APP_PASSWORD
DEFAULT_FROM_EMAIL=Livo OS <dev.digitalvint@gmail.com>

# --- Login security ---
OTP_LOGIN_REQUIRED=True
OTP_TTL_SECONDS=600
LOGIN_MAX_FAILURES=5
LOGIN_LOCKOUT_SECONDS=900
PASSWORD_MIN_LENGTH=10
TRUSTED_PROXY_DEPTH=1

# --- Notifications ---
# Email the assignee whenever a task is handed to them. Set False to fall back
# to the in-app bell alone.
TASK_ASSIGNMENT_EMAILS=True

# --- Uploads ---
MAX_UPLOAD_MB=5
IMAGE_MAX_EDGE=2000
IMAGE_QUALITY=80
USE_X_ACCEL_REDIRECT=True

# --- HTTPS: leave commented until Step 13 ---
# SECURE_SSL_REDIRECT=True
# SESSION_COOKIE_SECURE=True
# CSRF_COOKIE_SECURE=True
# SECURE_HSTS_SECONDS=31536000
# BEHIND_TLS_PROXY=True
# CSRF_TRUSTED_ORIGINS=https://za-an.com,https://www.za-an.com

# --- AI ---
GROQ_API_KEYS=YOUR_GROQ_KEY
GROQ_MODEL=llama-3.3-70b-versatile
EOF

chmod 600 /srv/livo_os/.env
chown livo:livo /srv/livo_os/.env
```

`TRUSTED_PROXY_DEPTH=1` tells the login throttle to read the real client IP from the
one proxy hop in front of it. Without it, every failed login looks like it came from
nginx, and one attacker could lock out the whole company.

---

## Step 8 — Build the database

```bash
cd /srv/livo_os
alias dj='sudo -u livo /srv/livo_os/.venv/bin/python manage.py'

dj check                     # should report no issues
dj migrate                   # creates every table and seeds the RBAC matrix
dj seed_doctypes             # the standard agency document types
dj collectstatic --noinput   # ~132 files into staticfiles/
```

Create the Super Admin. Give it a real address — **an account with no email cannot
sign in**, because the emailed code has nowhere to go:

```bash
dj createsuperuser --username admin --email contact.digitalvint@gmail.com
```

Then set the name so it matches your local setup:

```bash
dj shell -c "
from django.contrib.auth import get_user_model
U = get_user_model(); a = U.objects.get(username='admin')
a.first_name, a.last_name = 'Adeed', 'Zaan'
a.save(update_fields=['first_name','last_name'])
print(a.get_full_name(), a.email)
"
```

### Check who can see what

`migrate` also applies the row-level visibility rules. Two things are worth
confirming before anyone signs in, because both are easy to miss and awkward to
debug later:

```bash
dj shell -c "
from accounts.models import RolePermission
from projects.models import Project
print('view_all holders:')
for rp in RolePermission.objects.filter(can_view_all=True).select_related('role','module'):
    print(' ', rp.role.name, rp.module.key)
print('projects with no members:',
      Project.objects.filter(members__isnull=True).count())
"
```

Expect `view_all` on **projects** and **tasks** for Super Admin and Manager only.
A project with no members is invisible to everyone below Manager — fine for a
fresh install, a problem if you imported data. Add people on the project page
under **Team**, or let assignment do it: handing someone a task puts them on the
project automatically.

Analytics and Resource Planning are now Super Admin / Manager only. Both report
across every person's hours and task list, which is what the visibility rules
exist to contain. Grant them back per role in **/settings/roles/** if you need to.

Fix the media directory ownership (uploads are written by the app user):

```bash
mkdir -p /srv/livo_os/media
chown -R livo:livo /srv/livo_os/media /srv/livo_os/staticfiles
```

---

## Step 9 — Bring your existing data across *(optional)*

Skip this if you would rather start clean — your local database holds mostly demo data
(3 clients, 5 projects, 8 documents), and re-entering it is not a big job.

**On Windows**, export everything except the tables the server rebuilds itself:

```powershell
.\.venv\Scripts\python.exe manage.py dumpdata `
  --natural-foreign --natural-primary `
  --exclude contenttypes --exclude auth.permission `
  --exclude sessions.session --exclude admin.logentry `
  --indent 2 --output transfer.json
```

Copy it and the uploaded files up. **`media/` is gitignored, so git will not carry your
uploads — they must go separately:**

```powershell
scp transfer.json root@YOUR_VPS_IP:/tmp/
scp -r media/* root@YOUR_VPS_IP:/srv/livo_os/media/
```

On the server:

```bash
chown -R livo:livo /srv/livo_os/media
sudo -u livo /srv/livo_os/.venv/bin/python manage.py loaddata /tmp/transfer.json
rm /tmp/transfer.json
```

> If `loaddata` fails on a duplicate key, it is almost always the RBAC seed: both
> databases were seeded independently and the primary keys drifted. The quickest fix is
> to drop and recreate the database, run `migrate` **without** `seed_rbac`, and load
> again — or simply abandon the import and re-enter the data through the UI.

Passwords survive the transfer (Django exports the hash, not the password), so the
`admin` account keeps `Digitalvint@Admin@26#`.

---

## Step 10 — Run gunicorn under systemd

Find your web server's group first — nginx runs as `www-data` on Debian/Ubuntu and as
`nginx` on the RHEL family:

```bash
ps -eo user,group,comm | grep -E 'nginx|httpd' | head
```

Create `/etc/systemd/system/livo_os.service`:

```ini
[Unit]
Description=Livo OS (gunicorn)
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=notify
User=livo
Group=www-data
# ^ change to `nginx` on AlmaLinux/Rocky/CentOS

RuntimeDirectory=livo_os
RuntimeDirectoryMode=0755
WorkingDirectory=/srv/livo_os
Environment=DJANGO_SETTINGS_MODULE=config.settings
Environment=PYTHONUNBUFFERED=1

ExecStart=/srv/livo_os/.venv/bin/gunicorn \
    --workers 3 \
    --timeout 120 \
    --graceful-timeout 30 \
    --umask 007 \
    --access-logfile - \
    --error-logfile - \
    --bind unix:/run/livo_os/gunicorn.sock \
    config.wsgi:application

ExecReload=/bin/kill -s HUP $MAINPID
Restart=always
RestartSec=3
KillMode=mixed

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

> **`--timeout 120` is not optional.** AI drafting calls Groq with a 90-second timeout
> per key (`ai_engine/services.py`). Gunicorn's default of 30 seconds would kill the
> worker mid-request and hand the user a 502 in the middle of generating a document.
>
> **`--workers 3`** suits 1–2 vCPUs. The usual formula is `2 × nproc + 1`; raise it if
> `nproc` reported more.

Start it:

```bash
systemctl daemon-reload
systemctl enable --now livo_os
systemctl status livo_os --no-pager
```

Confirm the socket exists and answers:

```bash
ls -l /run/livo_os/gunicorn.sock
curl --unix-socket /run/livo_os/gunicorn.sock http://localhost/accounts/login/ -I
```

A `302` or `200` means the app is alive. If it fails: `journalctl -u livo_os -n 50 --no-pager`.

---

## Step 11 — nginx

**Add a new file. Do not edit the one that serves 4strokeinternational.com.**

- Debian/Ubuntu: `/etc/nginx/sites-available/za-an.com`, then symlink into `sites-enabled/`
- RHEL family: `/etc/nginx/conf.d/za-an.com.conf` (no symlink needed)

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name za-an.com www.za-an.com;

    access_log /var/log/nginx/za-an.access.log;
    error_log  /var/log/nginx/za-an.error.log;

    # The app refuses uploads over 5 MB itself and offers a Drive link instead.
    # Leave a little headroom so the app produces the friendly message rather
    # than nginx returning a bare 413.
    client_max_body_size 8M;

    location /static/ {
        alias /srv/livo_os/staticfiles/;
        expires 30d;
        access_log off;
    }

    # Uploaded files. `internal` means a browser can NEVER request this directly —
    # it is reachable only via the X-Accel-Redirect header that core/views_media.py
    # emits after checking the session. This is what stops invoices and employee
    # documents being downloadable by anyone who guesses a filename.
    location /protected/ {
        internal;
        alias /srv/livo_os/media/;
    }

    location / {
        proxy_pass http://unix:/run/livo_os/gunicorn.sock;
        proxy_set_header Host              $http_host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;

        # Matches gunicorn's 120s: AI generation can legitimately take ~90s.
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

Enable and reload — `nginx -t` first, every time:

```bash
# Debian/Ubuntu only:
ln -s /etc/nginx/sites-available/za-an.com /etc/nginx/sites-enabled/za-an.com

nginx -t          # MUST say "syntax is ok" and "test is successful"
systemctl reload nginx
```

`reload` is graceful — 4strokeinternational.com keeps serving throughout. If `nginx -t`
fails, **do not reload**; fix the file first. Nothing is broken until you reload.

Verify the existing site is still fine:

```bash
curl -I https://4strokeinternational.com
```

---

## Step 12 — Point za-an.com at the VPS (GoDaddy)

In GoDaddy: **My Products → Domains → za-an.com → DNS → Manage Zones**.

Add or edit these two records:

| Type | Name | Value | TTL |
|------|------|-------|-----|
| A | `@` | `YOUR_VPS_IPV4` | 600 seconds |
| CNAME | `www` | `za-an.com` | 600 seconds |

Delete any existing `A` record on `@` pointing somewhere else, and remove GoDaddy's
parking/forwarding entry for the domain if one exists — forwarding overrides DNS and
will silently keep sending visitors to a parking page.

Set TTL to 600 **before** you make the change if you can, so mistakes are cheap to fix.

Wait for propagation, then check from your machine:

```powershell
nslookup za-an.com 8.8.8.8
nslookup www.za-an.com 8.8.8.8
```

Both must return your VPS IP. This usually takes 10–30 minutes; occasionally longer.
**Do not run certbot until this resolves** — the certificate authority validates by
fetching a file over HTTP from the domain, so it fails if DNS is not live yet.

Once DNS resolves, the site should already answer over plain HTTP:

```bash
curl -I http://za-an.com/accounts/login/
```

---

## Step 13 — TLS certificate, then turn on the HTTPS switches

```bash
# Debian/Ubuntu
apt install -y certbot python3-certbot-nginx
# RHEL family
dnf install -y certbot python3-certbot-nginx

certbot --nginx -d za-an.com -d www.za-an.com \
        --agree-tos -m contact.digitalvint@gmail.com --redirect
```

Certbot edits your `za-an.com` server block in place, adds the 443 block, and installs
the HTTP→HTTPS redirect. It does not touch the other site. Renewal is automatic; prove
it works:

```bash
systemctl list-timers | grep certbot
certbot renew --dry-run
```

**Now** enable the HTTPS settings — not before, or the secure-only cookies will lock
you out. Uncomment the block in `/srv/livo_os/.env`:

```bash
sudo -u livo sed -i \
  -e 's/^# SECURE_SSL_REDIRECT=/SECURE_SSL_REDIRECT=/' \
  -e 's/^# SESSION_COOKIE_SECURE=/SESSION_COOKIE_SECURE=/' \
  -e 's/^# CSRF_COOKIE_SECURE=/CSRF_COOKIE_SECURE=/' \
  -e 's/^# SECURE_HSTS_SECONDS=/SECURE_HSTS_SECONDS=/' \
  -e 's/^# BEHIND_TLS_PROXY=/BEHIND_TLS_PROXY=/' \
  -e 's/^# CSRF_TRUSTED_ORIGINS=/CSRF_TRUSTED_ORIGINS=/' \
  /srv/livo_os/.env

systemctl restart livo_os
```

Confirm the app now considers itself production-safe:

```bash
sudo -u livo /srv/livo_os/.venv/bin/python manage.py check --deploy
```

Expect **at most one** warning, about `SECURE_HSTS_PRELOAD` — that one is optional and
only matters if you intend to submit the domain to the browser preload list.

> **HSTS is a one-way door.** `SECURE_HSTS_SECONDS=31536000` tells browsers to refuse
> plain HTTP to za-an.com for a year, and they cache that. Only leave it on once you are
> confident TLS is stable.

---

## Step 14 — Scheduled jobs

Three management commands need to run on a schedule; nothing runs them otherwise.
`send_client_updates` emails clients what changed on their content calendar — it
needs `SITE_URL` set in `.env`, or the emails go out without links.

```bash
crontab -u livo -e
```

```cron
# Livo OS
*/10 * * * * cd /srv/livo_os && .venv/bin/python manage.py send_calendar_reminders >> /var/log/livo_os-cron.log 2>&1
*/10 * * * * cd /srv/livo_os && .venv/bin/python manage.py send_client_updates >> /var/log/livo_os-cron.log 2>&1
5 1 * * *    cd /srv/livo_os && .venv/bin/python manage.py generate_recurring_tasks >> /var/log/livo_os-cron.log 2>&1
```

```bash
touch /var/log/livo_os-cron.log
chown livo:livo /var/log/livo_os-cron.log
```

---

## Step 15 — Email deliverability

Test that the server can actually reach Gmail. **Some VPS providers block outbound port
587 by default** — if this hangs, open a BigRock support ticket to unblock SMTP:

```bash
cd /srv/livo_os
sudo -u livo .venv/bin/python -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from django.core.mail import get_connection
c = get_connection(); c.open(); print('SMTP OK'); c.close()
"
```

Then add the SPF record BigRock asked for. In **GoDaddy DNS for za-an.com**:

| Type | Name | Value | TTL |
|------|------|-------|-----|
| TXT | `@` | `v=spf1 +a +mx include:eig.spf.a.cloudfilter.net include:_spf.google.com ~all` | 1 hour |

> BigRock's suggested value is `v=spf1 +a +mx include:eig.spf.a.cloudfilter.net ~all`.
> I have added `include:_spf.google.com` because your sign-in codes are sent **through
> Gmail's servers**, not the VPS — without it those messages fail SPF and land in spam.
> **A domain may have only one SPF record.** If za-an.com already has one, merge the
> `include:` entries into it rather than adding a second.

---

## Step 16 — Backups

A database with no backup is a database you are about to lose.

```bash
mkdir -p /var/backups/livo_os
cat > /usr/local/bin/livo-backup.sh <<'EOF'
#!/bin/bash
set -euo pipefail
STAMP=$(date +%F-%H%M)
DEST=/var/backups/livo_os

PGPASSWORD='DB_PASSWORD_HERE' pg_dump -h 127.0.0.1 -U livo_os livo_os \
  | gzip > "$DEST/db-$STAMP.sql.gz"

tar czf "$DEST/media-$STAMP.tar.gz" -C /srv/livo_os media

# keep 14 days
find "$DEST" -name '*.gz' -mtime +14 -delete
EOF

chmod 700 /usr/local/bin/livo-backup.sh
```

```bash
crontab -e
```
```cron
30 2 * * * /usr/local/bin/livo-backup.sh >> /var/log/livo-backup.log 2>&1
```

Test the restore path at least once — an untested backup is a guess:

```bash
/usr/local/bin/livo-backup.sh && ls -lh /var/backups/livo_os
```

Copy the archives off the server periodically. A backup that only exists on the machine
it is protecting is not a backup.

---

## Automatic deploys (GitHub Actions → VPS)

Push to `main` and the server updates itself. `.github/workflows/deploy.yml` runs a
smoke check, then opens one SSH connection to the VPS, which runs
`/usr/local/bin/agencyos-deploy`. About two minutes from push to live.

```
git push origin main
      │
      ├─ Smoke check (~1 min, GitHub runner, Python 3.10 as on the VPS)
      │    manage.py check --deploy
      │    manage.py makemigrations --check --dry-run
      │    manage.py collectstatic --noinput
      │        └─ fails here if a stylesheet points at a file that isn't in the repo
      │
      └─ Deploy (~40 s, on the VPS)
           git fetch + reset --hard origin/main
           pip install        ← only when requirements.txt changed
           migrate
           collectstatic
           systemctl restart agencyos
           health check ──── unhealthy ──► roll the code back, restart, exit 1
                    │
                   healthy → done
```

**The health check is the point.** After the restart the script asks gunicorn for
`/accounts/login/` over its unix socket, retrying for 30 seconds. If nothing answers
`200` or `302`, it puts the previous commit back and restarts again — so a commit
that fails to import takes the site down for seconds, not until somebody notices.

> **Rollback restores code, not the database.** Migrations are not reversed. Django
> migrations are normally additive, so the old code runs against the new schema
> fine; guessing at a backwards migration against live data is the more dangerous
> move. If the rollback is *also* unhealthy, suspect the schema and look at
> `journalctl -u agencyos -n 80 --no-pager`.

### How the CI key is restricted

The workflow holds a private key that can log in to the VPS as `root`, which sounds
worse than it is. Its entry in `/root/.ssh/authorized_keys` is:

```
command="/usr/local/bin/agencyos-deploy",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding ssh-ed25519 AAAA... github-actions-deploy@agencyos
```

`command=` is a **forced command**: sshd ignores whatever the client asks to run and
runs that script instead. Someone holding this key can deploy the current `main`.
They cannot get a shell, read `/root`, forward a port, or touch the other site on
this box. That is also why the workflow sends the word `deploy` and nothing else —
the argument is discarded, and it is there to make the log readable.

The key is **not** the root password, and the pipeline never uses a password.
Rotating the root password does not affect deploys.

### One-time setup

**1 — Create the key** (on your machine, not in the repo):

```bash
ssh-keygen -t ed25519 -N "" -C "github-actions-deploy@agencyos" -f ci_deploy
```

**2 — Install the public half on the VPS**, appending, never overwriting:

```bash
ssh root@66.116.242.39
cp -a /root/.ssh/authorized_keys /root/.ssh/authorized_keys.bak
printf '%s %s\n' \
  'command="/usr/local/bin/agencyos-deploy",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding' \
  "$(cat ci_deploy.pub)" >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
```

**3 — Install the deploy script**, from `deploy/agencyos-deploy` in this repo:

```bash
install -o root -g root -m 750 deploy/agencyos-deploy /usr/local/bin/agencyos-deploy
bash -n /usr/local/bin/agencyos-deploy && echo "syntax OK"
```

`deploy/agencyos-deploy` is the canonical copy. It does **not** update itself on a
deploy — editing it in the repo means copying it across again with the line above,
deliberately, because a script that rewrote itself mid-run is a bad way to find out
a change was wrong.

**4 — Add the private half to GitHub** — repo → Settings → Secrets and variables →
Actions → New repository secret:

| Name | Value |
|---|---|
| `VPS_SSH_KEY` | the whole of `ci_deploy`, `-----BEGIN` through `-----END OPENSSH PRIVATE KEY-----`, including the trailing newline |

That is the only secret. The host address and its public host key are not secrets and
are committed (`.github/known_hosts`), so moving to a new server is a reviewable
commit rather than a silent settings change.

**5 — Delete your local copy of the private key** once the secret is saved. GitHub
cannot show it to you again, and neither can anyone else who gets your laptop:

```bash
shred -u ci_deploy 2>/dev/null || rm -f ci_deploy
```

**6 — Test it** without waiting for a commit: Actions → Deploy → *Run workflow*.

### When something goes wrong

| Symptom | Where to look |
|---|---|
| Workflow red at *Smoke check* | The commit is broken; nothing was deployed. The step name says which check failed. |
| Workflow red at *Deploy* | The site is on the **old** code and up — the health check rolled it back. Read the step log; it prints both commit SHAs. |
| `Host key verification failed` | The VPS was rebuilt and has new host keys. Re-read `/etc/ssh/ssh_host_*_key.pub` into `.github/known_hosts`. |
| `Permission denied (publickey)` | The `VPS_SSH_KEY` secret is missing, truncated, or the public half is no longer in `authorized_keys`. |
| Deploy hangs | Another deploy holds the lock. The script waits 5 minutes, then gives up. |

Every run appends to `/var/log/agencyos-deploy.log` on the server, with both SHAs and
the health-check result, so the history survives GitHub's log retention:

```bash
tail -40 /var/log/agencyos-deploy.log
```

---

## Routine updates, by hand

The pipeline above is the normal path. This is what it automates, for when you need to
run it yourself — or just run `/usr/local/bin/agencyos-deploy`, which does all of it
with the health check and rollback.

Work on Windows, push, then on the server:

```bash
cd /srv/livo_os
sudo -u livo git pull
sudo -u livo .venv/bin/pip install -r requirements.txt   # only if deps changed
sudo -u livo .venv/bin/python manage.py migrate
sudo -u livo .venv/bin/python manage.py collectstatic --noinput
systemctl restart livo_os
```

Worth saving as `/usr/local/bin/livo-deploy.sh`:

```bash
#!/bin/bash
set -euo pipefail
cd /srv/livo_os
sudo -u livo git pull
sudo -u livo .venv/bin/pip install -q -r requirements.txt
sudo -u livo .venv/bin/python manage.py migrate --noinput
sudo -u livo .venv/bin/python manage.py collectstatic --noinput
systemctl restart livo_os
systemctl --no-pager status livo_os | head -5
```

---

## Final checklist

Work through this after Step 16.

- [ ] `https://za-an.com` loads the login page; `http://` redirects to it
- [ ] `https://www.za-an.com` works too
- [ ] **`https://4strokeinternational.com` still loads** ← check this explicitly
- [ ] The padlock is valid on both domains
- [ ] Sign in as `admin` → the six-digit code arrives at contact.digitalvint@gmail.com
- [ ] The dashboard, Clients, Projects, Documents, Analytics, Planning and Calendar pages all render with styling (proves `/static/` is served)
- [ ] Open a document with a generated PDF — it downloads (proves the media view works)
- [ ] **Log out, then paste that same PDF URL into a private window — it must redirect to the login page, not download.** This is the check that proves invoices are not public.
- [ ] Upload a file over 5 MB → the Drive-link prompt appears rather than an nginx 413
- [ ] Generate a document with AI → completes without a 502 (proves the 120s timeouts)
- [ ] `manage.py check --deploy` reports at most the HSTS-preload warning
- [ ] `systemctl is-enabled livo_os postgresql nginx` → all `enabled`
- [ ] `/usr/local/bin/livo-backup.sh` has produced files in `/var/backups/`
- [ ] Reboot the VPS once and confirm everything comes back by itself

---

## Troubleshooting

**502 Bad Gateway**
gunicorn is down or nginx cannot reach the socket.
`systemctl status livo_os` · `journalctl -u livo_os -n 80 --no-pager` ·
`ls -l /run/livo_os/gunicorn.sock`. If the socket exists but nginx still fails,
it is a permissions problem — check the `Group=` in the service file matches the nginx
user, or (RHEL) see Appendix C.

**502 only when generating a document with AI**
A timeout is still at its default. Both `--timeout 120` in the service file and
`proxy_read_timeout 120s` in nginx are required.

**"Bad Request (400)"**
The `Host` header is not in `ALLOWED_HOSTS`. Check the `.env` line reads
`ALLOWED_HOSTS=za-an.com,www.za-an.com` and restart the service.

**Pages load but look unstyled**
The `alias` path in nginx is wrong. Confirm
`/srv/livo_os/staticfiles/css/app.css` exists, then
`curl -I https://za-an.com/static/css/app.css`.

**"Missing staticfiles manifest entry for 'css/app.css'"**
`collectstatic` was not run for this release. Production hashes static filenames
(`app.c8e3fe30254c.css`) so a stylesheet change can never be masked by a cached
copy, and the trade is that `{% static %}` now fails loudly instead of pointing
at a file that may be stale. Run
`python manage.py collectstatic --noinput` and restart. Note this makes
collectstatic mandatory on **every** release that touches `static/`, not only
the first one.

**PDFs and images 404 while logged in**
`USE_X_ACCEL_REDIRECT=True` is missing from `.env`, or the `location /protected/`
block is absent. The `alias` must end with a trailing slash.

**CSRF verification failed**
`CSRF_TRUSTED_ORIGINS` must include the `https://` scheme, and `BEHIND_TLS_PROXY=True`
must be set so Django knows the original request was HTTPS.

**No sign-in code arrives**
`journalctl -u livo_os | grep -i mail`. Usually outbound 587 is blocked (Step 15)
or the Gmail app password is wrong. Emergency access: set `OTP_LOGIN_REQUIRED=False`,
restart, log in, fix email, then **turn it back on**.

**Locked out entirely**
```bash
sudo -u livo /srv/livo_os/.venv/bin/python manage.py changepassword admin
```

**Migrations fail with a permission error**
The PostgreSQL 15+ schema grant in Step 4 was missed.

---

## Appendix A — If Apache owns the ports instead of nginx

Do not install nginx; it would fight Apache for 80/443. Add a vhost at
`/etc/httpd/conf.d/za-an.com.conf` (RHEL) or `/etc/apache2/sites-available/za-an.com.conf`
(Debian) and bind gunicorn to a TCP port instead of a socket — change the service file's
`--bind` to `127.0.0.1:8001`.

```apache
<VirtualHost *:80>
    ServerName za-an.com
    ServerAlias www.za-an.com

    Alias /static/ /srv/livo_os/staticfiles/
    <Directory /srv/livo_os/staticfiles>
        Require all granted
    </Directory>

    ProxyPreserveHost On
    ProxyPass        /static/ !
    ProxyPass        / http://127.0.0.1:8001/ timeout=120
    ProxyPassReverse / http://127.0.0.1:8001/
    RequestHeader set X-Forwarded-Proto "http"
</VirtualHost>
```

Enable the modules, then use `certbot --apache` instead of `--nginx`:

```bash
a2enmod proxy proxy_http headers            # Debian
systemctl restart apache2                   # or httpd
```

**Apache has no `X-Accel-Redirect`.** Set `USE_X_ACCEL_REDIRECT=False` in `.env` — the
media view then streams files through Django itself. Slower, but the permission check
is identical, so uploads stay private. (`mod_xsendfile` is the faster equivalent if you
want to install it.)

---

## Appendix B — Installing a newer Python

Django 5.2 requires Python 3.10 or newer. If Step 0 showed something older:

**Ubuntu / Debian:**
```bash
add-apt-repository ppa:deadsnakes/ppa && apt update
apt install -y python3.12 python3.12-venv python3.12-dev
# then build the venv with it:
python3.12 -m venv /srv/livo_os/.venv
```

**AlmaLinux / Rocky 8 or 9:**
```bash
dnf install -y python3.12 python3.12-devel
python3.12 -m venv /srv/livo_os/.venv
```

Everything else in the guide is unchanged — the venv pins the interpreter.

---

## Appendix C — SELinux (RHEL family only)

AlmaLinux, Rocky and CentOS ship with SELinux enforcing, which will block nginx from
reaching the socket and reading your files. Symptoms are a 502 with
`(13: Permission denied)` in `/var/log/nginx/za-an.error.log` while the socket clearly
exists.

```bash
setsebool -P httpd_can_network_connect 1

dnf install -y policycoreutils-python-utils
semanage fcontext -a -t httpd_var_run_t "/run/livo_os(/.*)?"
semanage fcontext -a -t httpd_sys_content_t "/srv/livo_os/staticfiles(/.*)?"
semanage fcontext -a -t httpd_sys_content_t "/srv/livo_os/media(/.*)?"
restorecon -Rv /run/livo_os /srv/livo_os/staticfiles /srv/livo_os/media
```

Check what was actually denied before reaching for `setenforce 0`:

```bash
ausearch -m avc -ts recent
```

Leaving SELinux disabled on an internet-facing box removes a real layer of defence.
Fix the labels instead.

---

## Appendix D — What is intentionally not here

- **The `admin` password and the Groq key.** Both are configured in `.env`, which is
  gitignored and must be created on the server by hand. A credential in a tracked file
  is a credential in every clone.
- **A staging environment.** Worth adding later as a second systemd service, second
  database and a `staging.za-an.com` vhost, once the production flow is routine.
- **Per-file media authorisation.** The media view requires a *logged-in* user for
  every upload. It does not yet check that this particular user may see that particular
  file. Employee documents already have their own per-user gate in
  `employees/views.py`; other uploads are visible to any authenticated staff member.
  Tighten it if you ever give clients portal accounts.
