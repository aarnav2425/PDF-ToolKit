import subprocess, io, re, os, zipfile, tempfile, shutil, threading, http.server, functools, logging
from urllib.parse import urlparse, quote
from flask import Flask, request, send_file, jsonify, render_template, abort
import pymupdf as fitz  # PyMuPDF
from pypdf import PdfWriter

# All temporary files live in ./workspace inside the project, so disk use is visible here.
WORKSPACE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")
os.makedirs(WORKSPACE, exist_ok=True)
for _n in os.listdir(WORKSPACE):  # clear leftovers from a previous crash
    _p = os.path.join(WORKSPACE, _n)
    shutil.rmtree(_p, ignore_errors=True) if os.path.isdir(_p) else os.remove(_p)
tempfile.tempdir = WORKSPACE  # Python temp files, incl. large-upload spill files
for _v in ("TMPDIR", "TEMP", "TMP"):  # Chromium's browser profile
    os.environ[_v] = WORKSPACE

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
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
log = logging.getLogger(__name__)
SLOTS = threading.BoundedSemaphore(2)  # max concurrent Chromium renders


@app.before_request
def block_cross_origin_posts():
    # Stops other websites from silently triggering uploads/renders on your machine (CSRF)
    if request.method == "POST":
        o = request.headers.get("Origin")
        if o and urlparse(o).netloc != request.host:
            abort(403)


def safe_zip(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    infos = z.infolist()
    if len(infos) > 1000 or sum(i.file_size for i in infos) > 200 * 1024 * 1024:
        raise ValueError("ZIP has too many files or is too large when unpacked.")
    return z


def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def date_key(name):
    m = re.search(r"_(\d{8})\.html?\.zip$", name, re.I)
    return m.group(1) if m else "00000000"


def sort_files(files, mode):
    if mode == "natural":
        return sorted(files, key=lambda f: natural_key(f.filename))
    if mode == "date":
        return sorted(files, key=lambda f: date_key(f.filename))
    return files  # "manual": keep upload order


def reply(data, name, mime, original=None):
    r = send_file(io.BytesIO(data), mimetype=mime, as_attachment=True, download_name=name)
    r.headers["X-Result-Size"] = str(len(data))
    if original:
        r.headers["X-Original-Size"] = str(original)
    r.headers["Access-Control-Expose-Headers"] = "X-Result-Size, X-Original-Size, Content-Disposition"
    return r


def err(msg, code=400):
    return jsonify(error=msg), code


def dir_size(path):
    return sum(os.path.getsize(os.path.join(r, n)) for r, _, ns in os.walk(path) for n in ns
               if os.path.exists(os.path.join(r, n)))


@app.get("/api/disk-usage")
def disk_usage():
    return jsonify(bytes=dir_size(WORKSPACE), path=WORKSPACE)


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/api/merge-pdf")
def merge_pdf():
    files = sort_files(request.files.getlist("files"), request.form.get("sort", "manual"))
    if len(files) < 2:
        return err("Upload at least 2 PDF files.")
    w = PdfWriter()
    try:
        for f in files:
            w.append(io.BytesIO(f.read()))
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
    """Return list of (name, text) from .html/.htm files and .zip archives."""
    items = []
    for f in sort_files(files, mode):
        n = f.filename
        if n.lower().endswith(".zip"):
            with safe_zip(f.read()) as z:
                members = sorted([m for m in z.namelist() if m.lower().endswith((".htm", ".html"))], key=natural_key)
                items += [(f"{n}/{m}", z.read(m).decode("utf-8", "ignore")) for m in members]
        elif n.lower().endswith((".htm", ".html")):
            items.append((n, f.read().decode("utf-8", "ignore")))
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
            b = p.chromium.launch()
            ctx = b.new_context(java_script_enabled=False)
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
def html_to_pdf():
    files = request.files.getlist("files")
    if not files:
        return err("Upload an HTML file or a ZIP.")
    if not SLOTS.acquire(timeout=10):
        return err("Server busy, try again shortly.", 429)
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
        log.exception("html-to-pdf failed")
        return err("Conversion failed.", 500)
    finally:
        SLOTS.release()
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/compress-pdf")
def compress_pdf():
    f = request.files.get("file")
    if not f:
        return err("Upload a PDF.")
    raw = f.read()
    mode = request.form.get("mode", "raster")
    try:
        dpi = max(36, min(300, int(request.form.get("dpi", 150))))
        quality = max(1, min(95, int(request.form.get("quality", 70))))
    except ValueError:
        return err("DPI and quality must be numbers.")
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        if mode == "raster":  # same technique as compress.py, with tunable DPI/quality
            new = fitz.open()
            for page in doc:
                if page.rect.width * page.rect.height * (dpi / 72) ** 2 > 120e6:
                    return err("A page is too large to rasterize at this DPI; lower it.")
                img = page.get_pixmap(dpi=dpi).tobytes("jpeg", jpg_quality=quality)
                np_ = new.new_page(width=page.rect.width, height=page.rect.height)
                np_.insert_image(page.rect, stream=img)
            out = new.tobytes(garbage=4, deflate=True, clean=True)
        else:  # lossless: keeps text/vectors
            out = doc.tobytes(garbage=4, deflate=True, clean=True)
    except Exception as e:
        return err("Could not process this PDF.")
    return reply(out, "compressed.pdf", "application/pdf", original=len(raw))


@app.post("/api/split-pdf")
def split_pdf():
    f = request.files.get("file")
    spec = request.form.get("pages", "").strip()
    if not f or not spec:
        return err("Upload a PDF and enter pages, e.g. 1-3,5,8-10.")
    try:
        doc = fitz.open(stream=f.read(), filetype="pdf")
        order = []
        for part in spec.split(",")[:200]:
            a, _, b = part.strip().partition("-")
            lo, hi = max(int(a), 1), min(int(b or a), len(doc))
            order += [i - 1 for i in range(lo, hi + 1)]
        if not order:
            return err("No valid pages in range.")
        doc.select(order)
        return reply(doc.tobytes(garbage=3, deflate=True), "extracted.pdf", "application/pdf")
    except Exception as e:
        return err("Invalid page list or PDF.")


def patch_pymupdf_for_pdf2docx():
    """Older pdf2docx releases call Rect.get_area(), which some PyMuPDF releases no longer have.
    Add it back (same maths) so the two libraries work together whatever versions are installed."""
    for modname in ("pymupdf", "fitz"):  # pdf2docx imports `fitz`; cover both names
        try:
            mod = __import__(modname)
        except ImportError:
            continue
        for cls in (getattr(mod, "Rect", None), getattr(mod, "IRect", None)):
            if cls is not None and not hasattr(cls, "get_area"):
                cls.get_area = lambda self, unit="px": abs(self.width * self.height)


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@app.post("/api/pdf-to-word")
def pdf_to_word():
    f = request.files.get("file")
    if not f:
        return err("Upload a PDF.")
    if not SLOTS.acquire(timeout=10):
        return err("Server busy, try again shortly.", 429)
    tmp = tempfile.mkdtemp(dir=WORKSPACE)
    try:
        patch_pymupdf_for_pdf2docx()
        from pdf2docx import Converter
        src, dst = os.path.join(tmp, "input.pdf"), os.path.join(tmp, "output.docx")
        f.save(src)
        cv = Converter(src)
        try:
            cv.convert(dst)
        finally:
            cv.close()
        with open(dst, "rb") as fh:
            return reply(fh.read(), "converted.docx", DOCX)
    except ImportError:
        return err("pdf2docx is not installed. In the project folder run: python3 -m pip install -r requirements.txt", 501)
    except Exception:
        log.exception("pdf-to-word failed")
        return err("Could not convert this PDF (corrupt, encrypted or scanned?).", 500)
    finally:
        SLOTS.release()
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/word-to-pdf")
def word_to_pdf():
    f = request.files.get("file")
    ext = os.path.splitext(f.filename)[1].lower() if f else ""
    if ext not in (".doc", ".docx", ".odt", ".rtf"):
        return err("Upload a .doc, .docx, .odt or .rtf file.")
    office = find_office()
    if not office:
        return err("LibreOffice not found. Install it (macOS: brew install --cask libreoffice), then restart the app.", 501)
    head = f.read(8); f.seek(0)
    magic = {".docx": b"PK", ".odt": b"PK", ".doc": b"\xd0\xcf\x11\xe0", ".rtf": b"{\\rtf"}[ext]
    if not head.startswith(magic):
        return err(f"This doesn't look like a real {ext} file.")
    if not SLOTS.acquire(timeout=10):
        return err("Server busy, try again shortly.", 429)
    tmp = tempfile.mkdtemp(dir=WORKSPACE)
    try:
        src = os.path.join(tmp, "input" + ext)
        f.save(src)
        subprocess.run([office, "--headless", "--norestore", "--nolockcheck",
                        f"-env:UserInstallation=file://{os.path.join(tmp, 'profile')}",
                        "--convert-to", "pdf", "--outdir", tmp, src],
                       capture_output=True, timeout=120, check=True, env={**os.environ, "HOME": tmp})
        out = os.path.join(tmp, "input.pdf")
        if not os.path.exists(out):
            return err("Could not convert this document.", 500)
        with open(out, "rb") as fh:
            return reply(fh.read(), "converted.pdf", "application/pdf")
    except subprocess.TimeoutExpired:
        return err("Conversion timed out.", 504)
    except Exception:
        log.exception("word-to-pdf failed")
        return err("Could not convert this document.", 500)
    finally:
        SLOTS.release()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
