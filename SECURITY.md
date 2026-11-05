# Security

This app opens files uploaded by strangers and runs powerful programs on them (Chromium, LibreOffice, PDF parsers). Treat every upload as hostile. This page says what is protected, how, and what is still your job.

## What is being protected
1. **The documents people upload.** They may be confidential.
2. **Your server.** Its CPU, memory, disk and network position.
3. **The login**, when you set one.

## Who it defends against
- A visitor who uploads a booby-trapped file (ZIP bomb, Word document with outside links or macros, HTML that tries to read local files).
- Random people on the internet guessing the password or hammering the server.
- Another website that tries to make a visitor's browser use the app.
- Someone watching the network.

## Controls
| Threat | Control | Where |
|---|---|---|
| Password guessing | Per-IP lockout (8 wrong logins in 10 min, then 429), constant-time comparison of both username and password, optional **hashed** password (`APP_PASSWORD_HASH`) or password read from a file (`*_FILE`) | `guard`, `Lockout`, `credentials_ok` |
| Password or files read in transit | `PUBLIC_MODE` refuses plain HTTP; HSTS header over HTTPS; Caddy handles certificates | `guard`, `security_headers`, `Caddyfile` |
| Cross-site request forgery | Foreign `Origin` and `Sec-Fetch-Site: cross-site` POSTs are refused | `guard` |
| DNS rebinding, host-header tricks | Only your own host names answer (`ALLOWED_HOSTS`), also when a password is set | `guard` |
| Script injection (XSS) | CSP with a fresh nonce per page load and **no** `unsafe-inline` for scripts, `object-src 'none'`, `base-uri 'none'`; all file names escaped before display | `security_headers`, `index.html` |
| Clickjacking, sniffing, referrer leaks | `X-Frame-Options`, `frame-ancestors 'none'`, `nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, COOP | `security_headers` |
| HTML to PDF reading local files or internal services | JavaScript off, service workers blocked, every request except the job's own throwaway local server aborted, **and** the browser cannot resolve any other host name | `html_to_pdf_bytes` |
| ZIP bombs, oversized uploads | Caps: 1,000 files and 200 MB unpacked (also inside `.docx` / `.odt`), upload and page limits, small limit for non-file form fields | `safe_zip`, `MAX_*` |
| Word documents with external links or macros | Pre-scan before LibreOffice sees the file. It reads attributes the way an XML parser does (either quote style, any case, element prefixes, character references like `&#73;`, fields split across runs, ODF links with `..`, legacy `.doc` VBA projects) | `office_risk` |
| A crafted PDF that makes a parser loop forever | Every heavy job runs under a deadline (`JOB_SECONDS`). On timeout: 504, the slot is freed, and under gunicorn the worker restarts, so hung threads cannot starve the server | `run_with_deadline` |
| Memory exhaustion by small uploads that expand | Caps on ZIP unpack size, on HTML held by *Merge HTML*, on pixels per page in *Compress*, and on pages produced by *Extract pages*; those endpoints use the shared job slots | `safe_zip`, `collect_html`, `compress_bytes`, `split_pdf` |
| Crashing, slow or runaway converters | Separate processes with time limits, CPU-time and file-size caps, process-group kill, no core dumps | `run_isolated`, `pdf_worker.py` |
| One visitor using every slot | Per-visitor in-flight limit (`PER_IP_JOBS`), rate limits shared across workers through Redis, server-wide slot cap | `heavy_slot`, `rate` |
| Data left behind | Temporary jobs deleted after each run, workspace is a private RAM-backed folder in Docker, files created with mode 600, orphans swept after 1 hour, results are never stored on the server | `_reaper`, `docker-compose.yml` |
| Data leaking through logs | Tracebacks and converter output are never logged (they can quote document text); no file names or contents are logged; security events log the IP only | `log.error` calls |
| Converter bug phoning home | In Docker the app sits on an internal network with **no route to the internet** | `docker-compose.yml` |
| Container break-out | Non-root user, read-only filesystem, all capabilities dropped, `no-new-privileges`, memory / CPU / process limits, `noexec` temp folders | `docker-compose.yml` |
| Vulnerable dependencies | `pip-audit`, `bandit` and the tests run on every push and weekly; Dependabot opens update pull requests | `.github/` |

Run the tests with `python3 -m unittest discover -s tests -v`. Each of the controls above that can be checked without LibreOffice or Chromium has a test that fails on the unprotected code.

## Your checklist before going public
- [ ] Domain points at the server, `DOMAIN` is set, and only ports 80 and 443 are open. **Never publish port 8000**: the app trusts the proxy in front of it.
- [ ] A login is set (`APP_USER` plus `APP_PASSWORD_HASH`) if only chosen people should use it.
- [ ] `.env` and `secrets/` are in `.gitignore` and not in the repository.
- [ ] SSH uses keys only; the server gets automatic security updates.
- [ ] You rebuild the image regularly (`docker compose build --pull && docker compose up -d`) so Chromium, LibreOffice and Python get patches.
- [ ] You told users, on the page, that files are processed temporarily and deleted.
- [ ] You know where logs go and how long they are kept (they contain IP addresses). `ACCESS_LOG=0` turns off request logs.

## Limits you should know about
- **Chromium runs with `--no-sandbox` inside the container.** The container hardening, the network isolation and JavaScript-off rendering are the boundary instead. A determined attacker with a Chromium exploit would still have to escape the container. For higher assurance run conversions on a separate machine or under gVisor.
- The Word scan is a filter, not a guarantee. LibreOffice and the PDF libraries parse hostile input; keep them updated.
- A hung job's thread cannot be killed from inside Python. The deadline frees the slot and restarts the worker, but other requests on that worker are cut off when it restarts (after up to 30 s).
- Lockouts and the per-visitor job limit are kept **per worker process**. With 2 workers an attacker gets up to twice the guesses. Rate limits that go through Redis are shared.
- HTTP Basic authentication has no logout and no second factor. For more than a few trusted users, put a proper login in front (for example an identity-aware proxy).
- Anyone with the server's administrator access, or the hosting provider, can in principle read memory while a file is processed. For highly confidential documents keep the service private (for example Tailscale).
- Optional "Recent results" in the browser keeps files on the visitor's own device for up to 24 hours. Tell people not to enable it on shared computers.
- Not yet built: virus scanning of uploads, per-user accounts, audit trail, end-to-end encryption.

## If something goes wrong
1. Stop the service: `docker compose stop`.
2. Change the password (generate a new hash) and restart. Nothing else needs rotating, because the app stores no user data.
3. Check `docker compose logs app | grep -E "failed login|locked out|blocked"` for what happened.

## Reporting a vulnerability
Please tell the repository owner privately (not in a public issue) and give steps to reproduce.
