# Deploying Personal Tools for other people

This adds a public-server setup on top of the existing app. Local use (`python3 app.py`) works as before.

## What you need
- A Linux server (VPS) with **2 vCPU / 4 GB RAM or more** and Docker + the Compose plugin installed
- A domain name with an `A` record pointing at the server's IP
- Ports 80 and 443 open

## Steps
```bash
git clone <your repo> && cd Personal-Tools-
cp .env.example .env          # set DOMAIN=tools.yourdomain.com
docker compose up -d --build
docker compose logs -f app    # watch it start
```
Open `https://tools.yourdomain.com`. Caddy fetches the HTTPS certificate on its own (first load may take ~30 s).

Check it: `curl https://tools.yourdomain.com/healthz` should print `ok`.

## What changed, and why
| Problem when sharing | Change |
|---|---|
| Flask's built-in server is single-purpose and not for the public | Runs under **gunicorn** (2 worker processes x 4 threads) |
| Start-up wiped the whole workspace, which would delete other workers' running jobs | Wipe happens **once**, in `gunicorn.conf.py`; a background reaper removes orphaned jobs older than 1 hour |
| One shared password was the only protection | **Per-IP rate limits** (Flask-Limiter + Redis, shared across workers). Password stays optional for a private beta |
| Host check only allowed localhost | `ALLOWED_HOSTS=<your domain>`; `PUBLIC_MODE=1` refuses to start if it is missing |
| Behind a proxy every visitor looked like the proxy | `PROXY_HOPS=1` (ProxyFix) so limits and origin checks use the real client |
| Footer polled a disk-usage figure every 2 s per visitor, shared across all users | Hidden in public mode |
| 200 MB uploads x many visitors = out of memory | `MAX_UPLOAD_MB=50`, `MAX_PAGES=500`, container memory/CPU/process caps |
| Conversions run beside the web app with the host's privileges | Non-root user, read-only filesystem, all capabilities dropped, size-capped tmpfs workspace |
| No health probe | `/healthz` (exempt from login and limits) |

Environment variables you can set (all optional): `JOB_SECONDS`, `ZIP_MAX_MB`, `MAX_HTML_MB`, `MAX_RASTER_MP`, `APP_USER`, `APP_PASSWORD_HASH`, `LOGIN_MAX_FAILS`, `PER_IP_JOBS`, `ACCESS_LOG`, `ORGANIZER_MAX_PAGES`, `BATCH_MAX`, `BATCH_SECONDS`, `MAX_UPLOAD_MB`, `MAX_PAGES`, `SLOTS`, `WEB_CONCURRENCY`, `THREADS`, `RATE_HEAVY`, `RATE_DEFAULT`, `RATELIMIT_STORAGE_URI`, `PROXY_HOPS`, `PUBLIC_MODE`, `CHROMIUM_ARGS`, `WORKSPACE_DIR`, `PORT`, `BIND`.

## Tuning
Heavy jobs running at once = `WEB_CONCURRENCY x SLOTS`. Budget roughly **1 GB RAM and 1 CPU per heavy job** (Chromium and LibreOffice are hungry). Raise these together with `mem_limit`, `cpus` and the tmpfs size in `docker-compose.yml`. Extra requests wait 10 s for a slot, then get a "server busy" message.

## New tools and memory
The page organizer renders thumbnails on the server, and batch jobs hold several files in memory at once. If you raise `MAX_UPLOAD_MB`, `ORGANIZER_MAX_PAGES` or `BATCH_MAX`, raise `mem_limit` too. The browser history of results is stored on each visitor's own device, not on your server.

## Passwords and secrets
For a login, prefer a hash over a plain password:
```bash
mkdir -p secrets && chmod 700 secrets
python3 -c "from werkzeug.security import generate_password_hash as g; import getpass; print(g(getpass.getpass()))" > secrets/app_password_hash
chmod 600 secrets/app_password_hash
```
Then add to `docker-compose.yml`: under `app:` set `APP_PASSWORD_HASH_FILE: /run/secrets/app_password_hash` and `secrets: [app_password_hash]`, and at the bottom `secrets: {app_password_hash: {file: ./secrets/app_password_hash}}`. Keep `.env` and `secrets/` out of git.

## Security hardening in this setup
- **No internet for the app container.** It sits on an internal Docker network, so a converter bug cannot call out. (Only Caddy is reachable from outside.)
- HTTPS is required (`PUBLIC_MODE`), with HSTS, and the app refuses to start without a host list and the proxy setting.
- Login lockout, per-visitor job limits and Redis-backed rate limits.
- Read-only container, non-root, no capabilities, `noexec` temp folders, no core dumps, `init` process to reap converter children.
- CI runs `pip-audit`, `bandit` and the tests weekly. See `SECURITY.md` for the full list and your checklist.

## Things to know before going public
- **Chromium runs with `--no-sandbox`** inside the container (its own sandbox needs privileges Docker drops). The container hardening, JavaScript-off rendering and the network lock in `app.py` are what protect you. For stronger isolation, run the converters on a separate machine (see "Next step").
- **Cloudflare in front?** The free plan caps request time at about 100 s and upload size at 100 MB, so long PDF-to-Word jobs can fail. Set `PROXY_HOPS=2` if you put Cloudflare in front of Caddy.
- **Privacy:** people will upload personal documents. Files live only in memory or the workspace and are deleted after each job; say so on the page, and don't log filenames if you add analytics.
- **Keep updated:** `docker compose build --pull` regularly and run `pip-audit -r requirements.txt`. PDF/Office parsers handling strangers' files are your biggest attack surface.
- **Backups:** nothing to back up; the app stores no user data.

## Next step if usage grows: a job queue
Right now each request holds a connection open while the conversion runs. To scale past one server:
1. The web app only validates the upload and puts a job in Redis (RQ or Celery).
2. Separate **worker containers** run Chromium / LibreOffice / pdf2docx. They can sit on other machines, have no internet access, and be scaled up or down independently.
3. Results go to temporary storage (Redis or S3-style bucket) with a 15-minute expiry; the page polls `/api/jobs/<id>` and shows progress.
This also removes the proxy timeout problem. It needs changes to every endpoint and to the front-end script.
