# Production Deployment Guide — GCES No Due Portal

Target: self-hosted **Fedora Linux**, 12+ cores / 32 GB+ RAM. Goal: sustain
~200–400 concurrent request slots for public usage.

---

## 1. One-time setup

```bash
# System deps
sudo dnf install -y python3 python3-pip

# App deps
pip install -r requirements.txt

# Collect static files (WhiteNoise serves them)
python manage.py collectstatic --noinput

# Apply Django's own tables (sessions/admin live in SQLite)
python manage.py migrate

# Build MongoDB indexes (idempotent — safe to re-run any time)
python manage.py ensure_indexes
```

> If `ensure_indexes` logs *"Could not create index … duplicate values"*, the
> collection already contains duplicate `reg_no`/`roll_no` or two `no_due`
> docs for the same `(student, office)`. Clean those up, then re-run — the
> unique indexes are what enforce no-duplicate-processing at the DB level.

## 2. `.env`

```ini
MONGO_URI=mongodb+srv://...          # required
SECRET_KEY=<64+ random chars>        # required in prod — do NOT ship the fallback
DEBUG=False                          # MUST be False in production
CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...

# Optional tuning (sensible defaults exist for all of these)
WEB_CONCURRENCY=25          # gunicorn worker processes
WEB_THREADS=16              # threads per worker  → 25*16 = 400 slots
MONGO_MAX_POOL_SIZE=20      # per worker; 25 workers * 20 = 500 max DB sockets
LOG_LEVEL=INFO
MAINTENANCE_INTERVAL_SECONDS=60
```

## 3. Run

```bash
gunicorn -c gunicorn.conf.py nodue_portal.wsgi
```

The worker/thread counts auto-scale from the CPU count if the env vars are
unset. See `gunicorn.conf.py` for every knob.

### systemd service (`/etc/systemd/system/nodue.service`)

```ini
[Unit]
Description=GCES No Due Portal
After=network.target

[Service]
User=nodue
WorkingDirectory=/opt/nodue
EnvironmentFile=/opt/nodue/.env
ExecStart=/opt/nodue/venv/bin/gunicorn -c gunicorn.conf.py nodue_portal.wsgi
Restart=always
RestartSec=3
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nodue
journalctl -u nodue -f          # live logs
```

Put **nginx** in front for TLS termination, gzip, and static caching, then set
`CSRF_TRUSTED_ORIGINS` in `settings.py` to your real domain and keep
`SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` on (they follow `DEBUG=False`).

## 4. Background maintenance

Stale-PENDING expiry, cooldown clearing, and orphaned-receipt cleanup run
automatically **inside each worker** via a daemon thread, coordinated by a
MongoDB lock so only one worker does the work per tick. Nothing extra to run.

If you prefer an **external** scheduler instead, disable the in-process one and
use a systemd timer:

```ini
# /etc/systemd/system/nodue-maintenance.service
[Service]
Type=oneshot
EnvironmentFile=/opt/nodue/.env
Environment=DISABLE_BACKGROUND_MAINTENANCE=1
WorkingDirectory=/opt/nodue
ExecStart=/opt/nodue/venv/bin/python manage.py run_maintenance
```
```ini
# /etc/systemd/system/nodue-maintenance.timer
[Timer]
OnUnitActiveSec=60
[Install]
WantedBy=timers.target
```
(Also set `DISABLE_BACKGROUND_MAINTENANCE=1` in the main service's env.)

## 5. Load testing

```bash
# Raw capacity (no auth), from a SECOND machine:
python scripts/loadtest.py http://SERVER:8000/ -c 400 -d 30

# Realistic user journeys (needs a staging dataset):
pip install locust
locust -f loadtest/locustfile.py --host http://SERVER:8000
```

Increase concurrency until the error rate rises above ~1% or p95 latency
becomes unacceptable — the last stable level is your capacity ceiling.
