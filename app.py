import subprocess, io, re, os, zipfile, tempfile, shutil, threading, http.server, functools, logging, hmac, signal, sys, time, json, base64, math, contextlib, hashlib, secrets
from urllib.parse import urlparse, quote
from flask import Flask, request, send_file, jsonify, render_template, abort, Response, send_from_directory, g, copy_current_request_context
import pymupdf as fitz  # PyMuPDF
from pypdf import PdfWriter, PdfReader
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash
try:
    import resource  # Unix only
except ImportError:
    resource = None

def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _secret(name, default=""):
    """Value of $NAME, or the contents of the file named by $NAME_FILE (Docker / Kubernetes secrets keep
    passwords out of the environment, `ps` output and shell history)."""
    path = os.environ.get(name + "_FILE")
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read().rstrip("\r\n")
    return os.environ.get(name, default)


os.umask(0o077)  # every file this process creates is private to its user
if resource:
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no core dumps: they would contain document contents
    except (ValueError, OSError):
        pass

# All temporary files live in one folder (default ./workspace, override with WORKSPACE_DIR).
WORKSPACE = os.environ.get("WORKSPACE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")
os.makedirs(WORKSPACE, exist_ok=True)
if os.path.islink(WORKSPACE):  # the wipe below must never follow a symlink elsewhere
    raise SystemExit("workspace must be a real folder, not a symlink")
try:
    os.chmod(WORKSPACE, 0o700)  # other users on this computer can't browse it
except OSError:
    pass
tempfile.tempdir = WORKSPACE  # Python temp files, incl. large-upload spill files
for _v in ("TMPDIR", "TEMP", "TMP"):  # Chromium's browser profile
    os.environ[_v] = WORKSPACE


def _remove(p):
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p, ignore_errors=True)
    else:
        try:
            os.remove(p)
        except OSError:
            pass


def wipe_workspace():
    """Empty the workspace. Call it ONCE, from the process that launches the server (see gunicorn.conf.py),
    never from every worker: a worker that starts late would delete the jobs of the ones already running."""
    for n in os.listdir(WORKSPACE):
        _remove(os.path.join(WORKSPACE, n))


def _reaper(max_age=3600, every=300):
    """Safety net for jobs orphaned by a crashed or recycled worker: delete anything older than max_age."""
    while True:
        time.sleep(every)
        cutoff = time.time() - max_age
        try:
            for n in os.listdir(WORKSPACE):
                p = os.path.join(WORKSPACE, n)
                try:
                    if os.lstat(p).st_mtime < cutoff:
                        _remove(p)
                except OSError:
                    pass
        except OSError:
            pass


threading.Thread(target=_reaper, daemon=True).start()

def find_office():
    """LibreOffice: PATH first, then the usual macOS / Windows install locations."""
    return shutil.which("soffice") or shutil.which("libreoffice") or next((p for p in (
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        r"C:\\Program Files\\LibreOffice\\program\\soffice.exe") if os.path.exists(p)), None)


class _NoPoll(logging.Filter):  # keep the terminal free of the page's 2-second usage polling
    def filter(self, record):
        return "/api/disk-usage" not in record.getMessage()


logging.getLogger("werkzeug").addFilter(_NoPoll())

app = Flask(__name__)
PROXY_HOPS = _env_int("PROXY_HOPS", 0)  # number of trusted reverse proxies in front (Caddy/nginx = 1)
if PROXY_HOPS:  # so request.remote_addr / host / scheme are the real client's, not the proxy's
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=PROXY_HOPS, x_proto=PROXY_HOPS, x_host=PROXY_HOPS)
MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 200)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = 1_000_000  # non-file form fields (page lists, passwords) stay small
app.config["MAX_FORM_PARTS"] = 200
log = logging.getLogger(__name__)
SLOTS = threading.BoundedSemaphore(max(1, _env_int("SLOTS", 2)))  # heavy jobs at once, PER worker process


BASE = os.path.dirname(os.path.abspath(__file__))
MAX_PAGES = _env_int("MAX_PAGES", 2000)
ORGANIZER_MAX_PAGES = _env_int("ORGANIZER_MAX_PAGES", 200)  # page thumbnails are rendered server-side
BATCH_MAX = _env_int("BATCH_MAX", 20)                    # files per batch (Compress, Word to PDF)
BATCH_SECONDS = _env_int("BATCH_SECONDS", 170)           # stop starting new files after this long
JOB_SECONDS = _env_int("JOB_SECONDS", 300)               # hard limit for one request's heavy work (hung parser protection)
ZIP_MAX_MB = _env_int("ZIP_MAX_MB", 200)                 # most a ZIP / .docx may unpack to
MAX_HTML_MB = _env_int("MAX_HTML_MB", 60)                # most HTML text Merge HTML will hold in memory
MAX_RASTER_MP = _env_int("MAX_RASTER_MP", 120)           # megapixels per page when Compress redraws pages as images
RECYCLE_ON_HANG = os.environ.get("RECYCLE_ON_HANG") == "1"
PDF = "application/pdf"
APP_PASSWORD = _secret("APP_PASSWORD")  # set it to require a login (use when sharing)
APP_PASSWORD_HASH = _secret("APP_PASSWORD_HASH")  # better: a werkzeug hash, so no plain password sits on the server
AUTH_ON = bool(APP_PASSWORD or APP_PASSWORD_HASH)
if APP_PASSWORD_HASH and APP_PASSWORD_HASH.count("$") < 2:
    raise SystemExit("APP_PASSWORD_HASH is not a valid hash. Make one with: python3 -c \"from werkzeug.security import generate_password_hash as g; import getpass; print(g(getpass.getpass()))\"")
sec = logging.getLogger("security")  # security events: failed logins, blocked requests (no file contents, ever)
LOGIN_MAX_FAILS = _env_int("LOGIN_MAX_FAILS", 8)    # wrong logins allowed per IP ...
LOGIN_WINDOW = _env_int("LOGIN_WINDOW", 600)        # ... within this many seconds, then that IP is locked out
PER_IP_JOBS = _env_int("PER_IP_JOBS", 2)            # heavy jobs one visitor may run at the same time (per worker)
APP_USER = os.environ.get("APP_USER", "")  # optional: also require this exact username (empty = any username)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
EXTRA_HOSTS = [h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
SECRET_WORDS = ("PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL")

# --- public deployment settings -------------------------------------------------------------
PUBLIC_MODE = os.environ.get("PUBLIC_MODE") == "1"  # serving strangers: refuse unsafe configurations
CHROMIUM_ARGS = [a for a in os.environ.get("CHROMIUM_ARGS", "").split(",") if a]  # e.g. --no-sandbox in Docker
RATE_DEFAULT = os.environ.get("RATE_DEFAULT", "300/minute")        # every route, per client IP
RATE_HEAVY = os.environ.get("RATE_HEAVY", "10/minute;100/hour")    # every conversion endpoint, per client IP
try:  # pip install flask-limiter (plus redis for limits shared across workers)
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(get_remote_address, app=app, default_limits=[RATE_DEFAULT], headers_enabled=True,
                      storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"))
except ImportError:
    limiter = None
if PUBLIC_MODE and limiter is None:
    raise SystemExit("PUBLIC_MODE=1 needs flask-limiter (pip install -r requirements.txt)")
if PUBLIC_MODE and not (AUTH_ON or EXTRA_HOSTS):
    raise SystemExit("PUBLIC_MODE=1 needs ALLOWED_HOSTS=your.domain (or a password) so the host check can work")
if PUBLIC_MODE and PROXY_HOPS < 1:
    raise SystemExit("PUBLIC_MODE=1 needs PROXY_HOPS=1 (the HTTPS reverse proxy). Without it every visitor looks like one IP address, so limits and lockouts would hit everyone.")


def rate(fn):  # per-IP limit for conversion endpoints
    return limiter.limit(RATE_HEAVY)(fn) if limiter else fn


class Lockout:
    """Counts wrong logins per client IP (in this worker process) and locks an IP out after too many."""
    def __init__(self, limit, window):
        self.limit, self.window, self.fails, self.lock = limit, window, {}, threading.Lock()

    def _recent(self, ip, now):
        t = [x for x in self.fails.get(ip, ()) if now - x < self.window]
        if t:
            self.fails[ip] = t
        else:
            self.fails.pop(ip, None)
        return t

    def locked(self, ip):
        with self.lock:
            return len(self._recent(ip, time.monotonic())) >= self.limit

    def fail(self, ip):
        with self.lock:
            now = time.monotonic()
            if len(self.fails) > 10000:  # bound memory under a flood of addresses
                self.fails.clear()
            self.fails[ip] = self._recent(ip, now) + [now]

    def ok(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


lockout = Lockout(LOGIN_MAX_FAILS, LOGIN_WINDOW)
_verified = set()  # SHA-256 of credentials that already passed the deliberately slow hash check (memory only)


def credentials_ok(a):
    user_ok = hmac.compare_digest((a.username or "").encode(), APP_USER.encode()) if APP_USER else True
    pw = a.password or ""
    if APP_PASSWORD_HASH:
        key = hashlib.sha256(f"{a.username}\0{pw}".encode()).digest()
        if key in _verified:
            pw_ok = True
        else:
            pw_ok = check_password_hash(APP_PASSWORD_HASH, pw)
            if pw_ok and len(_verified) < 64:
                _verified.add(key)
    else:
        pw_ok = hmac.compare_digest(pw.encode(), APP_PASSWORD.encode())
    return user_ok and pw_ok  # both halves are always checked, so timing doesn't reveal which one was wrong


@app.before_request
def guard():
    if request.path in ("/healthz", "/manifest.webmanifest", "/sw.js") or request.path.startswith("/static/"):
        return  # health probe + the (non-sensitive) files a phone fetches without credentials to install the app
    if PUBLIC_MODE and not request.is_secure:  # the login and your files must never travel in the clear
        return err("HTTPS is required.", 400)
    ip = request.remote_addr or "?"
    if AUTH_ON:  # shared mode: login required for everything
        if lockout.locked(ip):
            sec.warning("login locked out ip=%s", ip)
            return Response("Too many failed logins. Try again later.", 429, {"Retry-After": str(LOGIN_WINDOW)})
        a = request.authorization
        if not (a and credentials_ok(a)):
            if a:  # a missing header is just the browser asking for the login box; only wrong guesses count
                lockout.fail(ip)
                sec.warning("failed login ip=%s", ip)
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Personal Tools"'})
        lockout.ok(ip)
    if not AUTH_ON or EXTRA_HOSTS:  # only answer to our own host names (blocks DNS-rebinding and host-header tricks)
        host = (urlparse("//" + request.host).hostname or "").lower()
        if host not in LOCAL_HOSTS and not any(host == h or (h.startswith(".") and host.endswith(h)) for h in EXTRA_HOSTS):
            sec.warning("blocked host header ip=%s", ip)
            abort(400)
    if request.method == "POST":  # stop other websites from triggering uploads (CSRF)
        o = request.headers.get("Origin")
        if request.headers.get("Sec-Fetch-Site") == "cross-site" or (o and urlparse(o).netloc != request.host):
            sec.warning("blocked cross-site POST ip=%s", ip)
            abort(403)


@app.after_request
def security_headers(r):
    h = r.headers
    n = g.get("nonce")  # set per request by the page route; lets our own inline script run and nothing injected
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("Cache-Control", "no-store")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()")
    h.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'" + (f" 'nonce-{n}'" if n else "") +
                 "; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; "
                 "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    if request.is_secure:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return r


@app.errorhandler(413)
def too_big(_):
    return err(f"File too large (limit {MAX_UPLOAD_MB} MB).", 413)


@app.errorhandler(429)
def too_many(_):
    return err("Too many requests. Please wait a minute and try again.", 429)


class Busy(Exception):
    pass


class TimedOut(Exception):
    pass


class Failed(Exception):
    pass


def _recycle_worker():
    """A job is stuck inside a parser that cannot be interrupted. Under gunicorn, ask this worker process to
    restart (the master starts a fresh one), so the stuck thread cannot pile up and starve the server."""
    sec.error("a job ran longer than %s s; recycling this worker", JOB_SECONDS)
    if RECYCLE_ON_HANG:
        os.kill(os.getpid(), signal.SIGTERM)


def run_with_deadline(fn, seconds):
    box = {}

    def target():
        try:
            box["r"] = fn()
        except BaseException as e:  # handed back to the request thread
            box["e"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        _recycle_worker()
        raise TimedOut()
    if "e" in box:
        raise box["e"]
    return box["r"]


_inflight, _inflight_lock = {}, threading.Lock()


@contextlib.contextmanager
def heavy_slot():
    """One slot of server capacity: at most SLOTS heavy jobs at once, and at most PER_IP_JOBS per visitor,
    so one person cannot occupy every slot."""
    ip = request.remote_addr or "?"
    with _inflight_lock:
        if _inflight.get(ip, 0) >= PER_IP_JOBS:
            raise Busy()
        _inflight[ip] = _inflight.get(ip, 0) + 1
    try:
        if not SLOTS.acquire(timeout=10):
            raise Busy()
        try:
            yield
        finally:
            SLOTS.release()
    finally:
        with _inflight_lock:
            _inflight[ip] -= 1
            if _inflight[ip] <= 0:
                del _inflight[ip]


def limited(fn):  # heavy work: capped per server and per visitor, so a few requests can't exhaust memory/CPU
    @functools.wraps(fn)
    def wrapper(*a, **k):
        ctxfn = copy_current_request_context(fn)
        try:
            with heavy_slot():
                return run_with_deadline(lambda: ctxfn(*a, **k), JOB_SECONDS)
        except Busy:
            return err("Server busy, try again shortly.", 429)
        except TimedOut:
            return err("This job took too long and was stopped.", 504)
    return wrapper


def clean_env():  # don't hand secrets (e.g. APP_PASSWORD) to converter programs
    return {k: v for k, v in os.environ.items() if not any(w in k.upper() for w in SECRET_WORDS)}


def run_isolated(cmd, timeout, env=None):
    """Run cmd in its own process group and return (exit code, ""). (Output is intentionally discarded.) On timeout the whole group is
    killed: LibreOffice starts child processes that would otherwise keep running."""
    # Output is discarded (never buffered in memory, never logged: it can contain document text).
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    if resource is not None and hasattr(resource, "prlimit"):  # Linux: cap CPU time and the size of any file written
        try:
            resource.prlimit(p.pid, resource.RLIMIT_CPU, (timeout + 30, timeout + 30))
            resource.prlimit(p.pid, resource.RLIMIT_FSIZE, (1 << 30, 1 << 30))
        except (OSError, ValueError):
            pass
    try:
        p.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(p.pid, signal.SIGKILL) if hasattr(os, "killpg") else p.kill()
        except (ProcessLookupError, PermissionError):
            pass
        p.wait()
        raise
    return p.returncode, ""


_CHARREF = re.compile(rb"&#(x[0-9a-fA-F]+|[0-9]+);")
_FIELD_MARKS = (b"includetext", b"includepicture", b"ddeauto", b"w:altchunk", b"w:subdoc")


def _unref(b):
    """Decode numeric character references (&#73; or &#x49;). XML parsers do, so the scan has to look at
    what LibreOffice will see, not at the raw bytes ("&#73;NCLUDETEXT" is INCLUDETEXT)."""
    def one(m):
        try:
            v = m.group(1)
            n = int(v[1:], 16) if v[:1] in (b"x", b"X") else int(v)
            return chr(n).encode("utf-8") if 0 < n < 0x110000 else b""
        except (ValueError, OverflowError):
            return b""
    return _CHARREF.sub(one, b)


def _attr(tag, name):
    """Value of an un-prefixed attribute (either quote style, any case), lower-cased; b'' when absent."""
    m = re.search(rb"(?<![\w:.\-])" + name + rb"\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag, re.I)
    if not m:
        return b""
    return (m.group(1) if m.group(1) is not None else m.group(2)).lower()


def _has_field_marks(x):
    x = _unref(x).lower()  # decode references first: &#73; becomes 'I', which lower-casing must then see
    instr = b"".join(re.findall(rb"<w:instrtext[^>]*>(.*?)</w:instrtext>", x, re.S))  # Word splits a field over several runs
    return any(m in x or m in instr for m in _FIELD_MARKS)


def office_risk(ext, data):
    """Heuristic pre-scan: refuse documents that pull in outside files/URLs or carry macros."""
    bad = "This document links to outside files or contains macros, which isn't allowed."
    if ext in (".docx", ".odt"):
        with safe_zip(data) as z:
            for n in z.namelist():
                low = n.lower()
                if low.startswith(("basic/", "scripts/", "macros/")) or "vbaproject" in low:
                    return bad
                if low.endswith(".rels"):
                    for tag in re.findall(rb"<(?:[\w.\-]+:)?Relationship\b[^>]*>", _unref(z.read(n)), re.I):
                        if _attr(tag, b"TargetMode") == b"external" and not _attr(tag, b"Type").endswith(b"/hyperlink"):
                            return bad
                elif low.startswith("word/") and low.endswith(".xml"):
                    if _has_field_marks(z.read(n)):
                        return bad
                elif low in ("content.xml", "styles.xml", "meta.xml", "settings.xml"):
                    x = _unref(z.read(n))
                    if b"text:section-source" in x.lower() or b"office:script" in x.lower():
                        return bad
                    for m in re.finditer(rb"<([\w.:\-]+)\b([^>]*)>", x):
                        if m.group(1).lower() in (b"text:a", b"draw:a"):  # ordinary hyperlinks are fine
                            continue
                        href = _attr(m.group(2), b"xlink:href")
                        if href and not (href.startswith((b"pictures/", b"object", b"./object", b"#"))
                                         and b".." not in href and b":" not in href):
                            return bad
        return None
    low = data.lower()
    marks = [b"includetext", b"includepicture", b"ddeauto", b"\\objautlink", b"\\objlink"]
    marks += [m.decode().encode("utf-16le") for m in marks[:3]]
    if ext == ".doc":  # legacy binary Word: a VBA project shows up as this storage name (UTF-16)
        marks.append("_vba_project".encode("utf-16le"))
    return bad if any(m in low for m in marks) else None


def safe_zip(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    infos = z.infolist()
    if len(infos) > 1000 or sum(i.file_size for i in infos) > ZIP_MAX_MB * 1024 * 1024:
        raise ValueError("ZIP has too many files or is too large when unpacked.")
    return z


def natural_key(name):
    """Sort key for 'file 2' before 'file 10'. Tuples keep numbers and text comparable, and names such as
    '²' or a 5,000-digit number can no longer crash the sort."""
    return [(0, int(t), "") if t.isdecimal() and len(t) < 18 else (1, 0, t.lower()) for t in re.split(r"(\d+)", name)]


def date_key(name):
    m = re.search(r"_(\d{8})\.html?\.zip$", name, re.I)
    return m.group(1) if m else "00000000"


def sort_files(files, mode):
    if mode == "natural":
        return sorted(files, key=lambda f: natural_key(f.filename))
    if mode == "date":
        return sorted(files, key=lambda f: date_key(f.filename))
    return files  # "manual": keep upload order


def reply(data, name, mime, original=None, extra=None):
    r = send_file(io.BytesIO(data), mimetype=mime, as_attachment=True, download_name=name)
    r.headers["X-Result-Size"] = str(len(data))
    if original:
        r.headers["X-Original-Size"] = str(original)
    for k, v in (extra or {}).items():
        r.headers[k] = str(v)
    r.headers["Access-Control-Expose-Headers"] = "X-Result-Size, X-Original-Size, X-Batch-Done, X-Batch-Failed, Content-Disposition"
    return r


def err(msg, code=400):
    return jsonify(error=msg), code


def open_pdf(raw):
    """Open an uploaded PDF or raise ValueError with a message that is safe to show the user."""
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception:
        raise ValueError("This doesn't look like a valid PDF.")
    if doc.needs_pass:
        raise ValueError("This PDF is password-protected. Use Unlock PDF first.")
    if len(doc) > MAX_PAGES:
        raise ValueError(f"Too many pages (limit {MAX_PAGES}).")
    return doc


def uploaded_files():
    """Accepts both field names, so single-file and batch uploads share one code path."""
    fs = request.files.getlist("files") or request.files.getlist("file")
    return [f for f in fs if f and f.filename]


class Names:
    """Safe, unique file names for entries of a result ZIP."""
    def __init__(self):
        self.used = set()

    def make(self, filename, suffix, ext):
        base = os.path.splitext(os.path.basename((filename or "file").replace("\\", "/")))[0]
        base = re.sub(r"[^\w.\- ()]+", "_", base)[:80] or "file"
        name, i = base + suffix + ext, 2
        while name.lower() in self.used:
            name, i = f"{base}{suffix}_{i}{ext}", i + 1
        self.used.add(name.lower())
        return name


def zip_reply(done, failed, original=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in done:
            z.writestr(n, d)
        if failed:
            z.writestr("_errors.txt", "\n".join(f"{n}: {m}" for n, m in failed) + "\n")
    return reply(buf.getvalue(), "results.zip", "application/zip", original,
                 {"X-Batch-Done": len(done), "X-Batch-Failed": len(failed)})


def dir_size(path):
    return sum(os.path.getsize(os.path.join(r, n)) for r, _, ns in os.walk(path) for n in ns
               if os.path.exists(os.path.join(r, n)))


@app.get("/api/disk-usage")
def disk_usage():
    if PUBLIC_MODE:  # a global figure is meaningless (and an os.walk per visitor every 2 s is costly) when shared
        return jsonify(bytes=None)
    return jsonify(bytes=dir_size(WORKSPACE))


@app.get("/healthz")
def healthz():
    return "ok"


if limiter:
    limiter.exempt(healthz)


@app.route("/")
def index():
    g.nonce = secrets.token_urlsafe(16)
    return render_template("index.html", nonce=g.nonce)


STATIC = os.path.join(BASE, "static")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(STATIC, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/sw.js")  # must live at the site root so its scope covers the whole app
def service_worker():
    r = send_from_directory(STATIC, "sw.js", mimetype="text/javascript")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.post("/api/merge-pdf")
@rate
@limited
def merge_pdf():
    files = sort_files(request.files.getlist("files"), request.form.get("sort", "manual"))
    if len(files) < 2:
        return err("Upload at least 2 PDF files.")
    w = PdfWriter()
    try:
        pages = 0
        for f in files:
            data = f.read()
            pages += len(PdfReader(io.BytesIO(data)).pages)
            if pages > MAX_PAGES:
                return err(f"Too many pages (limit {MAX_PAGES}).")
            w.append(io.BytesIO(data))
    except Exception as e:
        return err("Could not read one of the PDFs (corrupt or encrypted?).")
    out = io.BytesIO()
    w.write(out)
    return reply(out.getvalue(), "merged.pdf", "application/pdf")


def html_parts(html):
    low, styles, i = html.lower(), [], 0
    while True:
        a = low.find("<style", i)
        b = low.find("</style>", a) if a >= 0 else -1
        if b < 0:
            break
        styles.append(html[a:b + 8]); i = b + 8
    a = low.find("<body")
    if a >= 0:
        a = low.find(">", a) + 1
        b = low.rfind("</body>")
        body = html[a:b if b > a else len(html)]
    else:
        body = re.sub(r"</?(html|head)[^>]*>|<!doctype[^>]*>", "", html, flags=re.I)
    return "\n".join(styles), body


def cname(n):  # make a filename safe inside an HTML comment
    return n.replace("--", "- -").replace(">", "&gt;")


def collect_html(files, mode):
    """Return list of (name, text) from .html/.htm files and .zip archives (at most MAX_HTML_MB of text)."""
    items, total, cap = [], 0, MAX_HTML_MB * 1024 * 1024
    for f in sort_files(files, mode):
        n = f.filename
        if n.lower().endswith(".zip"):
            with safe_zip(f.read()) as z:
                members = sorted([m for m in z.namelist() if m.lower().endswith((".htm", ".html"))], key=natural_key)
                for m in members:
                    total += z.getinfo(m).file_size  # declared size, checked BEFORE anything is read
                    if total > cap:
                        raise ValueError(f"Too much HTML to merge at once (limit {MAX_HTML_MB} MB).")
                    items.append((f"{n}/{m}", z.read(m).decode("utf-8", "ignore")))
        elif n.lower().endswith((".htm", ".html")):
            data = f.read()
            total += len(data)
            if total > cap:
                raise ValueError(f"Too much HTML to merge at once (limit {MAX_HTML_MB} MB).")
            items.append((n, data.decode("utf-8", "ignore")))
    return items


def merge_html_text(items, structure):
    if structure == "raw":  # same behaviour as the original share.py / share1.py
        return "".join(f"\n<!-- {cname(n)} -->\n{t}" for n, t in items)
    styles, bodies = [], []
    for n, t in items:
        s, b = html_parts(t)
        styles.append(s)
        bodies.append(f"<!-- {cname(n)} -->\n<section class='merged-doc'>{b}</section>")
    sep = "<style>.merged-doc{page-break-after:always}</style>"
    return f"<!DOCTYPE html>\n<html><head><meta charset='utf-8'>{sep}\n" + "\n".join(styles) + "\n</head><body>\n" + "\n".join(bodies) + "\n</body></html>"


@app.post("/api/merge-html")
@rate
@limited
def merge_html():
    try:
        items = collect_html(request.files.getlist("files"), request.form.get("sort", "natural"))
    except (ValueError, zipfile.BadZipFile) as e:
        return err(str(e) if isinstance(e, ValueError) else "Invalid ZIP file.")
    if not items:
        return err("No .html/.htm files (or ZIPs containing them) found.")
    text = merge_html_text(items, request.form.get("structure", "valid"))
    return reply(text.encode("utf-8"), "merged_output.html", "text/html")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def html_to_pdf_bytes(root, rel):
    """Render root/rel in Chromium with JS off and network locked to a throwaway
    local server that only serves `root`. file:// and all other hosts are blocked."""
    from playwright.sync_api import sync_playwright
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=root))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{srv.server_port}/"
    try:
        with sync_playwright() as p:
            # Defence in depth: besides the request filter below, the browser cannot even resolve other host names.
            b = p.chromium.launch(args=CHROMIUM_ARGS + ["--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
                                                        "--disable-extensions"])
            ctx = b.new_context(java_script_enabled=False, service_workers="block", accept_downloads=False)
            ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(origin) else r.abort())
            pg = ctx.new_page()
            pg.set_default_timeout(30000)
            pg.goto(origin + quote(rel.replace(os.sep, "/")))
            pg.emulate_media(media="print")
            pdf = pg.pdf(format="A4", print_background=True,
                         margin={k: "10mm" for k in ("top", "bottom", "left", "right")})
            b.close()
        return pdf
    finally:
        srv.shutdown(); srv.server_close()


@app.post("/api/html-to-pdf")
@rate
@limited
def html_to_pdf():
    files = request.files.getlist("files")
    if not files:
        return err("Upload an HTML file or a ZIP.")
    tmp = tempfile.mkdtemp(dir=WORKSPACE)
    try:
        if request.form.get("merge_first") == "1" or len(files) > 1:
            items = collect_html(files, request.form.get("sort", "natural"))
            if not items:
                return err("No HTML found.")
            rel = "merged.html"
            open(os.path.join(tmp, rel), "w", encoding="utf-8").write(merge_html_text(items, "valid"))
        else:
            f = files[0]
            if f.filename.lower().endswith(".zip"):  # zip with assets: extract, use first html
                with safe_zip(f.read()) as z:
                    z.extractall(tmp)
                found = [os.path.join(r, n) for r, _, ns in os.walk(tmp) for n in ns if n.lower().endswith((".htm", ".html"))]
                if not found:
                    return err("No HTML inside the ZIP.")
                rel = os.path.relpath(sorted(found, key=natural_key)[0], tmp)
            else:
                rel = "input.html"
                f.save(os.path.join(tmp, rel))
        return reply(html_to_pdf_bytes(tmp, rel), "output.pdf", "application/pdf")
    except (ValueError, zipfile.BadZipFile) as e:
        return err(str(e) if isinstance(e, ValueError) else "Invalid ZIP file.")
    except Exception:
        log.error("html-to-pdf failed")  # no traceback: messages can quote document content
        return err("Conversion failed.", 500)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def compress_bytes(raw, mode, dpi, quality):
    doc = open_pdf(raw)
    if mode == "raster":  # same technique as compress.py, with tunable DPI/quality
        new = fitz.open()
        for page in doc:
            if page.rect.width * page.rect.height * (dpi / 72) ** 2 > MAX_RASTER_MP * 1e6:
                raise ValueError("A page is too large to rasterize at this DPI; lower it.")
            img = page.get_pixmap(dpi=dpi).tobytes("jpeg", jpg_quality=quality)
            np_ = new.new_page(width=page.rect.width, height=page.rect.height)
            np_.insert_image(page.rect, stream=img)
        return new.tobytes(garbage=4, deflate=True, clean=True)
    return doc.tobytes(garbage=4, deflate=True, clean=True)  # lossless: keeps text/vectors


@app.post("/api/compress-pdf")
@rate
@limited
def compress_pdf():
    files = uploaded_files()
    if not files:
        return err("Upload a PDF.")
    if len(files) > BATCH_MAX:
        return err(f"Too many files at once (limit {BATCH_MAX}).")
    mode = request.form.get("mode", "raster")
    try:
        dpi = max(36, min(300, int(request.form.get("dpi", 150))))
        quality = max(1, min(95, int(request.form.get("quality", 70))))
    except ValueError:
        return err("DPI and quality must be numbers.")
    done, failed, total_in, names = [], [], 0, Names()
    deadline = time.monotonic() + BATCH_SECONDS
    for f in files:
        if time.monotonic() > deadline:
            failed.append((f.filename, "skipped: time limit reached, upload it again"))
            continue
        raw = f.read()
        total_in += len(raw)
        try:
            done.append((names.make(f.filename, "_compressed", ".pdf"), compress_bytes(raw, mode, dpi, quality)))
        except ValueError as e:
            failed.append((f.filename, str(e)))
        except Exception:
            log.error("compress failed")
            failed.append((f.filename, "Could not process this PDF."))
    if len(files) == 1:
        if not done:
            return err(failed[0][1])
        return reply(done[0][1], "compressed.pdf", PDF, original=total_in)
    if not done:
        return err("None of the files could be processed.")
    return zip_reply(done, failed, total_in)


@app.post("/api/split-pdf")
@rate
@limited
def split_pdf():
    f = request.files.get("file")
    spec = request.form.get("pages", "").strip()
    if not f or not spec:
        return err("Upload a PDF and enter pages, e.g. 1-3,5,8-10.")
    try:
        doc = open_pdf(f.read())
        order = []
        for part in spec.split(",")[:200]:
            a, _, b = part.strip().partition("-")
            lo, hi = max(int(a), 1), min(int(b or a), len(doc))
            order += [i - 1 for i in range(lo, hi + 1)]
            if len(order) > MAX_PAGES:  # "1-2000,1-2000,..." must not build a million-page file
                return err(f"That would create more than {MAX_PAGES} pages.")
        if not order:
            return err("No valid pages in range.")
        doc.select(order)
        return reply(doc.tobytes(garbage=3, deflate=True), "extracted.pdf", "application/pdf")
    except ValueError as e:
        msg = str(e)
        return err(msg if msg.startswith(("This ", "Too many")) else "Invalid page list or PDF.")
    except Exception:
        return err("Invalid page list or PDF.")


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@app.post("/api/pdf-to-word")
@rate
@limited
def pdf_to_word():
    f = request.files.get("file")
    if not f:
        return err("Upload a PDF.")
    tmp = tempfile.mkdtemp(dir=WORKSPACE)
    try:
        src, dst = os.path.join(tmp, "input.pdf"), os.path.join(tmp, "output.docx")
        f.save(src)
        rc, errtxt = run_isolated([sys.executable, os.path.join(BASE, "pdf_worker.py"), src, dst, str(MAX_PAGES)],
                                  180, clean_env())
        if rc == 3:
            return err("pdf2docx is not installed. In the project folder run: python3 -m pip install -r requirements.txt", 501)
        if rc == 4:
            return err(f"Too many pages (limit {MAX_PAGES}).")
        if rc != 0 or not os.path.exists(dst):
            log.error("pdf-to-word worker failed (exit %s)", rc)
            return err("Could not convert this PDF (corrupt, encrypted or scanned?).", 500)
        with open(dst, "rb") as fh:
            return reply(fh.read(), "converted.docx", DOCX)
    except subprocess.TimeoutExpired:
        return err("Conversion timed out.", 504)
    except Exception:
        log.error("pdf-to-word failed")
        return err("Could not convert this PDF (corrupt, encrypted or scanned?).", 500)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def office_to_pdf(office, name, data):
    """Convert one document with LibreOffice. Raises ValueError (bad input), Busy, TimedOut or Failed."""
    ext = os.path.splitext(name)[1].lower()
    if ext not in (".doc", ".docx", ".odt", ".rtf"):
        raise ValueError("Upload a .doc, .docx, .odt or .rtf file.")
    magic = {".docx": b"PK", ".odt": b"PK", ".doc": b"\xd0\xcf\x11\xe0", ".rtf": b"{\\rtf"}[ext]
    if not data.startswith(magic):
        raise ValueError(f"This doesn't look like a real {ext} file.")
    try:
        reason = office_risk(ext, data)
    except (ValueError, zipfile.BadZipFile) as e:
        raise ValueError(str(e) if isinstance(e, ValueError) else "Invalid document file.")
    if reason:
        raise ValueError(reason)
    with heavy_slot():
        return _office_convert(office, ext, data)


def _office_convert(office, ext, data):
    tmp = tempfile.mkdtemp(dir=WORKSPACE)
    try:
        src = os.path.join(tmp, "input" + ext)
        with open(src, "wb") as fh:
            fh.write(data)
        rc, _ = run_isolated([office, "--headless", "--norestore", "--nolockcheck",
                              f"-env:UserInstallation=file://{os.path.join(tmp, 'profile')}",
                              "--convert-to", "pdf", "--outdir", tmp, src],
                             120, {**clean_env(), "HOME": tmp})
        out = os.path.join(tmp, "input.pdf")
        if rc != 0 or not os.path.exists(out):
            raise Failed("Could not convert this document.")
        with open(out, "rb") as fh:
            return fh.read()
    except subprocess.TimeoutExpired:
        raise TimedOut()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _exc_info(e):  # (message shown to the user, HTTP status)
    if isinstance(e, (ValueError, Failed)):
        return str(e), 400 if isinstance(e, ValueError) else 500
    if isinstance(e, Busy):
        return "Server busy, try again shortly.", 429
    if isinstance(e, TimedOut):
        return "Conversion timed out.", 504
    return "Could not convert this document.", 500


@app.post("/api/word-to-pdf")
@rate
def word_to_pdf():
    files = uploaded_files()
    if not files:
        return err("Upload a .doc, .docx, .odt or .rtf file.")
    if len(files) > BATCH_MAX:
        return err(f"Too many files at once (limit {BATCH_MAX}).")
    office = find_office()
    if not office:
        return err("LibreOffice not found. Install it (macOS: brew install --cask libreoffice), then restart the app.", 501)
    done, failed, names, first_exc = [], [], Names(), None
    deadline = time.monotonic() + BATCH_SECONDS
    for f in files:
        if time.monotonic() > deadline:
            failed.append((f.filename, "skipped: time limit reached, upload it again"))
            continue
        try:
            done.append((names.make(f.filename, "", ".pdf"), office_to_pdf(office, f.filename, f.read())))
        except Exception as e:
            if not isinstance(e, (ValueError, Failed, Busy, TimedOut)):
                log.error("word-to-pdf failed")
            first_exc = first_exc or e
            failed.append((f.filename, _exc_info(e)[0]))
    if len(files) == 1:
        if not done:
            return err(*_exc_info(first_exc))
        return reply(done[0][1], "converted.pdf", PDF)
    if not done:
        return err("None of the files could be converted.", _exc_info(first_exc)[1])
    return zip_reply(done, failed)


# ---------------------------------------------------------------------------------------------
# Page tools: thumbnails / organizer, protect / unlock, watermark, page numbers
# ---------------------------------------------------------------------------------------------
def _one_pdf():
    f = request.files.get("file")
    return f.read() if f and f.filename else None


@app.post("/api/pdf-thumbs")
@rate
@limited
def pdf_thumbs():
    """Page previews as small JPEGs (base64). limit=0 means every page (organizer); limit=N the first N."""
    raw = _one_pdf()
    if raw is None:
        return err("Upload a PDF.")
    try:
        width = max(80, min(600, int(request.form.get("width", 160))))
        limit = max(0, int(request.form.get("limit", 0)))
    except ValueError:
        return err("Invalid size.")
    try:
        doc = open_pdf(raw)
        n = len(doc)
        if limit == 0 and n > ORGANIZER_MAX_PAGES:
            return err(f"This PDF has {n} pages; the page organizer handles up to {ORGANIZER_MAX_PAGES}. "
                       "Use Extract pages to cut it down first.")
        shown = min(n, limit or n, ORGANIZER_MAX_PAGES)
        thumbs = []
        for i in range(shown):
            pg = doc[i]
            z = width / max(pg.rect.width, 1.0)
            pix = pg.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
            thumbs.append({"w": pix.width, "h": pix.height,
                           "b64": base64.b64encode(pix.tobytes("jpeg", jpg_quality=65)).decode()})
        return jsonify(pages=n, thumbs=thumbs)
    except ValueError as e:
        return err(str(e))
    except Exception:
        log.error("pdf-thumbs failed")
        return err("Could not read this PDF.")


@app.post("/api/organize-pdf")
@rate
@limited
def organize_pdf():
    """ops = JSON list of [source page index, extra clockwise rotation in degrees], in the new order.
    Pages left out are deleted; a page listed twice is duplicated."""
    raw = _one_pdf()
    if raw is None:
        return err("Upload a PDF.")
    try:
        ops = [(int(a), int(b)) for a, b in json.loads(request.form.get("ops", "[]"))]
    except (ValueError, TypeError):
        return err("Invalid page list.")
    if not ops:
        return err("Keep at least one page.")
    if len(ops) > MAX_PAGES:
        return err(f"Too many pages (limit {MAX_PAGES}).")
    try:
        doc = open_pdf(raw)
        if any(not (0 <= p < len(doc)) or r % 90 for p, r in ops):
            return err("Invalid page list.")
        new = fitz.open()
        for p, r in ops:  # one insert per page, so duplicated pages stay independent of each other
            new.insert_pdf(doc, from_page=p, to_page=p, rotate=(doc[p].rotation + r) % 360)
        try:
            new.set_metadata(doc.metadata)
        except Exception:
            pass
        return reply(new.tobytes(garbage=3, deflate=True), "organized.pdf", PDF)
    except ValueError as e:
        return err(str(e))
    except Exception:
        log.error("organize-pdf failed")
        return err("Could not save this PDF.")


@app.post("/api/protect-pdf")
@rate
@limited
def protect_pdf():
    raw = _one_pdf()
    pw = request.form.get("password", "")
    if raw is None:
        return err("Upload a PDF.")
    if not 4 <= len(pw) <= 128:
        return err("Choose a password of 4 to 128 characters.")
    try:
        doc = open_pdf(raw)
        out = doc.tobytes(garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=pw, owner_pw=pw)
        return reply(out, "protected.pdf", PDF)
    except ValueError as e:
        return err(str(e))
    except Exception:
        log.error("protect-pdf failed")
        return err("Could not protect this PDF.")


@app.post("/api/unlock-pdf")
@rate
@limited
def unlock_pdf():
    """Removes the password from a PDF the user can already open (they must supply the password)."""
    raw = _one_pdf()
    pw = request.form.get("password", "")
    if raw is None:
        return err("Upload a PDF.")
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception:
        return err("This doesn't look like a valid PDF.")
    try:
        if not doc.is_encrypted:
            return err("This PDF isn't password-protected.")
        if doc.needs_pass and not doc.authenticate(pw):
            return err("Wrong password.")
        if len(doc) > MAX_PAGES:
            return err(f"Too many pages (limit {MAX_PAGES}).")
        return reply(doc.tobytes(garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE), "unlocked.pdf", PDF)
    except Exception:
        log.error("unlock-pdf failed")
        return err("Could not unlock this PDF.")


def _latin1(text):
    try:
        text.encode("latin-1")
        return True
    except UnicodeEncodeError:
        return False


WM_COLORS = {"gray": (0.5, 0.5, 0.5), "red": (0.8, 0.1, 0.1), "blue": (0.1, 0.3, 0.85), "black": (0, 0, 0)}


@app.post("/api/watermark-pdf")
@rate
@limited
def watermark_pdf():
    raw = _one_pdf()
    text = request.form.get("text", "").strip()
    if raw is None:
        return err("Upload a PDF.")
    if not text or len(text) > 60:
        return err("Enter watermark text (up to 60 characters).")
    if not _latin1(text):
        return err("Watermark text supports Latin letters, digits and common symbols only.")
    try:
        angle = 45 if request.form.get("angle", "45") == "45" else 0
        opacity = max(5, min(100, int(request.form.get("opacity", 25)))) / 100
    except ValueError:
        return err("Invalid options.")
    color = WM_COLORS.get(request.form.get("color", "gray"), WM_COLORS["gray"])
    try:
        doc = open_pdf(raw)
        unit = fitz.get_text_length(text, fontname="helv", fontsize=1)
        th = math.radians(angle)
        for page in doc:
            w, h = page.rect.width, page.rect.height
            fs = max(8.0, min(200.0, (math.hypot(w, h) if angle else w) * 0.6 / max(unit, 0.01)))
            vx, vy = -unit * fs / 2, 0.3 * fs  # text start relative to the visible page centre
            # Work in what the viewer shows, then map to the page's stored (unrotated) coordinates.
            vis = fitz.Point(w / 2 + math.cos(th) * vx + math.sin(th) * vy, h / 2 - math.sin(th) * vx + math.cos(th) * vy)
            start = vis * page.derotation_matrix
            ph = th + math.radians(page.rotation)  # compensate for the page's own /Rotate
            mat = fitz.Matrix(math.cos(ph), -math.sin(ph), math.sin(ph), math.cos(ph), 0, 0)
            page.insert_text(start, text, fontsize=fs, fontname="helv", color=color,
                             fill_opacity=opacity, stroke_opacity=opacity, morph=(start, mat), overlay=True)
        return reply(doc.tobytes(garbage=3, deflate=True), "watermarked.pdf", PDF)
    except ValueError as e:
        return err(str(e))
    except Exception:
        log.error("watermark-pdf failed")
        return err("Could not add the watermark.")


@app.post("/api/page-numbers")
@rate
@limited
def page_numbers():
    raw = _one_pdf()
    if raw is None:
        return err("Upload a PDF.")
    try:
        start_at = max(-999, min(99999, int(request.form.get("start", 1))))
        fs = max(6, min(36, int(request.form.get("size", 11))))
    except ValueError:
        return err("Invalid options.")
    style = request.form.get("format", "n")
    pos = request.form.get("position", "bc")
    if style not in ("n", "page", "of", "slash") or pos not in ("bl", "bc", "br", "tl", "tc", "tr"):
        return err("Invalid options.")
    try:
        doc = open_pdf(raw)
        last = start_at + len(doc) - 1
        for i, page in enumerate(doc):
            num = start_at + i
            label = {"n": f"{num}", "page": f"Page {num}", "of": f"Page {num} of {last}", "slash": f"{num} / {last}"}[style]
            tw = fitz.get_text_length(label, fontname="helv", fontsize=fs)
            w, h = page.rect.width, page.rect.height
            x = {"l": 36, "c": (w - tw) / 2, "r": w - 36 - tw}[pos[1]]
            y = h - 24 if pos[0] == "b" else 24 + fs
            page.insert_text(fitz.Point(x, y) * page.derotation_matrix, label, fontsize=fs, fontname="helv",
                             color=(0.2, 0.2, 0.2), rotate=page.rotation)
        return reply(doc.tobytes(garbage=3, deflate=True), "numbered.pdf", PDF)
    except ValueError as e:
        return err(str(e))
    except Exception:
        log.error("page-numbers failed")
        return err("Could not add page numbers.")


if __name__ == "__main__":  # local use. For a public server run gunicorn instead (see DEPLOY.md)
    wipe_workspace()
    app.run(host="127.0.0.1", port=_env_int("PORT", 5000), debug=False)
