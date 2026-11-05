"""Security tests for Personal Tools. Run:  python3 -m unittest discover -s tests -v

They cover the logic that protects the server (login, lockout, headers, CSRF, host checks, HTTPS, per-visitor
limits, ZIP and Word-document scans, file names). They do not need LibreOffice or Chromium.
"""
import atexit, base64, importlib, io, logging, os, shutil, sys, tempfile, unittest, zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    import pymupdf  # noqa: F401
except ImportError:  # lets the security tests run where the PDF library isn't installed
    sys.modules["pymupdf"] = mock.MagicMock()
try:
    import flask_limiter  # noqa: F401
    HAVE_LIMITER = True
except ImportError:  # minimal stand-in, only so PUBLIC_MODE can be exercised without the package
    fl = mock.MagicMock()
    fl.Limiter.return_value.limit.side_effect = lambda *a, **k: (lambda fn: fn)
    sys.modules["flask_limiter"] = fl
    sys.modules["flask_limiter.util"] = mock.MagicMock()
    HAVE_LIMITER = False

ENV = ["APP_PASSWORD", "APP_USER", "APP_PASSWORD_HASH", "APP_PASSWORD_FILE", "APP_PASSWORD_HASH_FILE", "ALLOWED_HOSTS",
       "PUBLIC_MODE", "PROXY_HOPS", "LOGIN_MAX_FAILS", "PER_IP_JOBS", "RATELIMIT_STORAGE_URI", "MAX_UPLOAD_MB",
       "ZIP_MAX_MB", "MAX_HTML_MB", "JOB_SECONDS", "RECYCLE_ON_HANG", "MAX_PAGES"]


_MADE = []  # temporary workspaces created by the tests, removed when the run ends
atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True) for d in _MADE])
logging.getLogger("security").setLevel(logging.CRITICAL)  # the tests trigger failed logins on purpose; keep the output quiet


def load_app(**env):
    """Import a fresh copy of app.py with the given environment variables.
    (app.py reads its settings when it is imported, so each test group needs its own fresh import.)"""
    for k in ENV:
        os.environ.pop(k, None)
    tempfile.tempdir = None  # a previous import pointed Python's temp folder at its own workspace
    ws = tempfile.mkdtemp(prefix="pt-test-")
    _MADE.append(ws)
    os.environ["WORKSPACE_DIR"] = ws
    os.environ.update({k: str(v) for k, v in env.items()})
    sys.modules.pop("app", None)
    return importlib.import_module("app")


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


class Headers(unittest.TestCase):
    def test_csp_uses_nonce_and_no_inline_scripts(self):
        c = load_app().app.test_client()
        r1, r2 = c.get("/"), c.get("/")
        csp = r1.headers["Content-Security-Policy"]
        script = [p for p in csp.split(";") if p.strip().startswith("script-src")][0]
        self.assertIn("'nonce-", script)
        self.assertNotIn("unsafe-inline", script)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        nonce = script.split("'nonce-")[1].split("'")[0]
        self.assertIn(f'nonce="{nonce}"', r1.get_data(as_text=True))
        self.assertNotEqual(nonce, r2.headers["Content-Security-Policy"].split("'nonce-")[1].split("'")[0])

    def test_other_headers(self):
        h = load_app().app.test_client().get("/").headers
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertIn("camera=()", h["Permissions-Policy"])
        self.assertEqual(h["Cache-Control"], "no-store")
        self.assertNotIn("Strict-Transport-Security", h)  # only over HTTPS


class Login(unittest.TestCase):
    def test_password_and_username(self):
        c = load_app(APP_USER="aarnav", APP_PASSWORD="correct-horse").app.test_client()
        self.assertEqual(c.get("/").status_code, 401)
        self.assertEqual(c.get("/", headers=basic("aarnav", "wrong")).status_code, 401)
        self.assertEqual(c.get("/", headers=basic("bob", "correct-horse")).status_code, 401)
        self.assertEqual(c.get("/", headers=basic("aarnav", "correct-horse")).status_code, 200)

    def test_any_username_when_none_set(self):
        c = load_app(APP_PASSWORD="correct-horse").app.test_client()
        self.assertEqual(c.get("/", headers=basic("whoever", "correct-horse")).status_code, 200)

    def test_lockout_after_repeated_failures(self):
        c = load_app(APP_PASSWORD="correct-horse", LOGIN_MAX_FAILS=3).app.test_client()
        for _ in range(3):
            self.assertEqual(c.get("/", headers=basic("x", "guess")).status_code, 401)
        r = c.get("/", headers=basic("x", "correct-horse"))  # even the right password is refused now
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)

    def test_missing_header_does_not_count_as_a_failure(self):
        c = load_app(APP_PASSWORD="correct-horse", LOGIN_MAX_FAILS=3).app.test_client()
        for _ in range(6):
            self.assertEqual(c.get("/").status_code, 401)
        self.assertEqual(c.get("/", headers=basic("x", "correct-horse")).status_code, 200)

    def test_hashed_password(self):
        from werkzeug.security import generate_password_hash
        h = generate_password_hash("s3cret-pass", method="pbkdf2:sha256:1000")
        c = load_app(APP_PASSWORD_HASH=h).app.test_client()
        self.assertEqual(c.get("/", headers=basic("u", "nope")).status_code, 401)
        self.assertEqual(c.get("/", headers=basic("u", "s3cret-pass")).status_code, 200)
        self.assertEqual(c.get("/", headers=basic("u", "s3cret-pass")).status_code, 200)  # served from the verified cache

    def test_invalid_hash_refuses_to_start(self):
        with self.assertRaises(SystemExit):
            load_app(APP_PASSWORD_HASH="plaintext-not-a-hash")

    def test_password_from_file(self):
        fd, path = tempfile.mkstemp()
        os.write(fd, b"file-secret\n"); os.close(fd)
        c = load_app(APP_PASSWORD_FILE=path).app.test_client()
        self.assertEqual(c.get("/", headers=basic("u", "file-secret")).status_code, 200)
        self.assertEqual(c.get("/", headers=basic("u", "file-secret\n")).status_code, 401)

    def test_public_assets_need_no_login(self):
        c = load_app(APP_PASSWORD="correct-horse").app.test_client()
        for path in ("/manifest.webmanifest", "/sw.js", "/static/icons/icon-192.png", "/healthz"):
            r = c.get(path); r.close()
            self.assertEqual(r.status_code, 200, path)
        self.assertEqual(c.get("/api/disk-usage").status_code, 401)

    def test_lockout_class(self):
        a = load_app()
        lo = a.Lockout(2, 60)
        self.assertFalse(lo.locked("1.1.1.1")); lo.fail("1.1.1.1"); lo.fail("1.1.1.1")
        self.assertTrue(lo.locked("1.1.1.1")); self.assertFalse(lo.locked("2.2.2.2"))
        lo.ok("1.1.1.1"); self.assertFalse(lo.locked("1.1.1.1"))


class HostAndCsrf(unittest.TestCase):
    def test_unknown_host_refused_without_password(self):
        c = load_app().app.test_client()
        self.assertEqual(c.get("/", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(c.get("/", headers={"Host": "localhost:5000"}).status_code, 200)

    def test_allowed_hosts_enforced_even_with_password(self):
        c = load_app(APP_PASSWORD="pw-pw-pw", ALLOWED_HOSTS="tools.example.com").app.test_client()
        h = basic("u", "pw-pw-pw")
        self.assertEqual(c.get("/", headers={**h, "Host": "evil.example"}).status_code, 400)
        self.assertEqual(c.get("/", headers={**h, "Host": "tools.example.com"}).status_code, 200)

    def test_password_alone_accepts_tunnel_host_names(self):
        c = load_app(APP_PASSWORD="pw-pw-pw").app.test_client()
        self.assertEqual(c.get("/", headers={**basic("u", "pw-pw-pw"), "Host": "random-words.trycloudflare.com"}).status_code, 200)

    def test_cross_site_posts_refused(self):
        c = load_app().app.test_client()
        self.assertEqual(c.post("/api/merge-html", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(c.post("/api/merge-html", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertNotEqual(c.post("/api/merge-html", headers={"Origin": "http://localhost"}).status_code, 403)


class PublicMode(unittest.TestCase):
    PUB = dict(PUBLIC_MODE=1, PROXY_HOPS=1, ALLOWED_HOSTS="tools.example.com")

    def test_refuses_unsafe_configuration(self):
        with self.assertRaises(SystemExit):
            load_app(PUBLIC_MODE=1, PROXY_HOPS=1)                       # no host list and no password
        with self.assertRaises(SystemExit):
            load_app(PUBLIC_MODE=1, ALLOWED_HOSTS="tools.example.com")  # no reverse proxy configured

    def test_https_required_and_hsts(self):
        c = load_app(**self.PUB).app.test_client()
        host = {"Host": "tools.example.com"}
        self.assertEqual(c.get("/", headers=host).status_code, 400)
        r = c.get("/", headers={**host, "X-Forwarded-Proto": "https"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("max-age", r.headers["Strict-Transport-Security"])
        self.assertEqual(c.get("/healthz").status_code, 200)  # health probe works without HTTPS

    def test_disk_usage_hidden(self):
        c = load_app(**self.PUB).app.test_client()
        r = c.get("/api/disk-usage", headers={"Host": "tools.example.com", "X-Forwarded-Proto": "https"})
        self.assertIsNone(r.get_json()["bytes"])


class Fairness(unittest.TestCase):
    def test_one_visitor_cannot_take_every_slot(self):
        a = load_app()
        a.PER_IP_JOBS = 1
        with a.app.test_request_context(environ_base={"REMOTE_ADDR": "9.9.9.9"}):
            with a.heavy_slot():
                with self.assertRaises(a.Busy):
                    with a.heavy_slot():
                        pass
            with a.heavy_slot():  # released again afterwards
                pass
        with a.app.test_request_context(environ_base={"REMOTE_ADDR": "9.9.9.9"}):
            with a.heavy_slot():
                with a.app.test_request_context(environ_base={"REMOTE_ADDR": "8.8.8.8"}):
                    with a.heavy_slot():  # a different visitor is unaffected
                        pass


class Uploads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = load_app()

    @staticmethod
    def zipped(files):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            for n, d in files.items():
                z.writestr(n, d)
        return b.getvalue()

    def test_zip_with_too_many_files_refused(self):
        with self.assertRaises(ValueError):
            self.a.safe_zip(self.zipped({f"{i}.html": "x" for i in range(1001)}))

    def test_zip_declaring_huge_size_refused(self):
        z = mock.MagicMock(); z.infolist.return_value = [mock.Mock(file_size=150 * 2**20), mock.Mock(file_size=150 * 2**20)]
        with mock.patch.object(self.a.zipfile, "ZipFile", return_value=z):
            with self.assertRaises(ValueError):
                self.a.safe_zip(b"")

    def test_word_scan(self):
        a = self.a
        rel = '<Relationships><Relationship Id="1" Type="x/%s" Target="%s" TargetMode="External"/></Relationships>'
        ok_link = self.zipped({"word/_rels/document.xml.rels": rel % ("hyperlink", "https://example.com"), "word/document.xml": "<w:p/>"})
        ext_img = self.zipped({"word/_rels/document.xml.rels": rel % ("image", "file:///etc/passwd"), "word/document.xml": "<w:p/>"})
        macro = self.zipped({"word/vbaProject.bin": "x", "word/document.xml": "<w:p/>"})
        include = self.zipped({"word/document.xml": "<w:instrText>INCLUDETEXT c:\\secret.txt</w:instrText>"})
        self.assertIsNone(a.office_risk(".docx", ok_link))
        for bad in (ext_img, macro, include):
            self.assertIsNotNone(a.office_risk(".docx", bad))
        self.assertIsNotNone(a.office_risk(".rtf", b"{\\rtf1 {\\field INCLUDETEXT x}}"))

    def test_zip_entry_names_cannot_escape(self):
        n = self.a.Names()
        self.assertEqual(n.make("../../etc/passwd", "_c", ".pdf"), "passwd_c.pdf")
        self.assertEqual(n.make("..\\..\\evil.pdf", "_c", ".pdf"), "evil_c.pdf")
        self.assertEqual(n.make("a<b>|c?.pdf", "", ".pdf"), "a_b_c_.pdf")
        self.assertEqual([n.make("same.pdf", "", ".pdf"), n.make("same.pdf", "", ".pdf")], ["same.pdf", "same_2.pdf"])

    def test_secrets_not_passed_to_converters(self):
        with mock.patch.dict(os.environ, {"APP_PASSWORD_HASH": "x", "MY_API_TOKEN": "y", "AWS_SECRET_ACCESS_KEY": "z"}):
            env = self.a.clean_env()
        self.assertFalse({"APP_PASSWORD_HASH", "MY_API_TOKEN", "AWS_SECRET_ACCESS_KEY"} & set(env))
        self.assertIn("PATH", env)

    def test_upload_limit_enforced(self):
        a = load_app(MAX_UPLOAD_MB=1)
        r = a.app.test_client().post("/api/merge-html", data={"files": (io.BytesIO(b"x" * 2_000_000), "a.html")},
                                     content_type="multipart/form-data")
        self.assertEqual(r.status_code, 413)

    def test_no_core_dumps(self):
        try:
            import resource
        except ImportError:
            self.skipTest("no resource module on this platform")
        load_app()
        self.assertEqual(resource.getrlimit(resource.RLIMIT_CORE), (0, 0))


class WordScanBypasses(unittest.TestCase):
    """The Word / OpenDocument pre-scan must not be fooled by re-spelling the same dangerous thing."""

    @classmethod
    def setUpClass(cls):
        cls.a = load_app()

    zipped = staticmethod(Uploads.zipped)

    def rels(self, tag):
        return self.zipped({"word/_rels/document.xml.rels": f"<Relationships>{tag}</Relationships>", "word/document.xml": "<w:p/>"})

    def test_external_relationships_in_every_spelling(self):
        for label, tag in {
            "single quotes": "<Relationship Id='1' Type='x/image' Target='http://e/x.png' TargetMode='External'/>",
            "spaces around =": '<Relationship Id="1" Type="x/image" Target="http://e/x.png" TargetMode = "External"/>',
            "lower case": '<Relationship Id="1" Type="x/image" Target="http://e/x.png" targetmode="external"/>',
            "word hyperlink hidden in Target": '<Relationship Id="1" Type="x/image" Target="http://e/hyperlink" TargetMode="External"/>',
            "element prefix": '<r:Relationship Id="1" Type="x/image" Target="http://e/x.png" TargetMode="External"/>',
            "character reference": '<Relationship Id="1" Type="x/image" Target="http://e/x.png" TargetMode="&#69;xternal"/>',
            "prefixed decoy Type": '<Relationship foo:Type="x/hyperlink" Type="x/image" TargetMode="External" Target="http://e"/>',
        }.items():
            self.assertIsNotNone(self.a.office_risk(".docx", self.rels(tag)), label)

    def test_ordinary_hyperlink_still_allowed(self):
        tag = '<Relationship Id="1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com" TargetMode="External"/>'
        self.assertIsNone(self.a.office_risk(".docx", self.rels(tag)))

    def test_include_field_split_or_encoded(self):
        def doc(xml): return self.zipped({"word/document.xml": xml})
        split = "<w:r><w:instrText>INCLU</w:instrText></w:r><w:r><w:instrText>DETEXT c:\\secret.txt</w:instrText></w:r>"
        for label, xml in {"split over runs": split, "character reference": "<w:instrText>&#73;NCLUDETEXT x</w:instrText>",
                           "hex reference": "<w:instrText>&#x49;NCLUDEPICTURE x</w:instrText>", "plain": "<w:instrText>INCLUDETEXT x</w:instrText>"}.items():
            self.assertIsNotNone(self.a.office_risk(".docx", doc(xml)), label)
        self.assertIsNone(self.a.office_risk(".docx", doc("<w:p><w:r><w:t>Hello world</w:t></w:r></w:p>")))

    def test_odt_links(self):
        def odt(xml): return self.zipped({"content.xml": xml})
        for label, xml in {"parent folder": '<draw:image xlink:href="Pictures/../../../etc/passwd"/>',
                           "single quotes": "<draw:image xlink:href='file:///etc/passwd'/>",
                           "any other element": '<draw:plugin xlink:href="http://evil.example/x"/>',
                           "template link": '<meta:template xlink:href="http://evil.example/t.ott"/>'}.items():
            self.assertIsNotNone(self.a.office_risk(".odt", odt(xml)), label)
        self.assertIsNone(self.a.office_risk(".odt", odt('<draw:image xlink:href="Pictures/a.png"/><text:a xlink:href="https://example.com">x</text:a>')))

    def test_legacy_doc_with_vba_project(self):
        ole = b"\xd0\xcf\x11\xe0" + b"\0" * 64
        self.assertIsNotNone(self.a.office_risk(".doc", ole + "_VBA_PROJECT".encode("utf-16le")))
        self.assertIsNone(self.a.office_risk(".doc", ole + "plain text".encode("utf-16le")))


class Robustness(unittest.TestCase):
    def test_sort_key_never_crashes(self):
        a = load_app()
        names = ["file\u00b2.pdf", "file2.pdf", "file10.pdf", "x" + "9" * 5000 + ".pdf", "\u0663.pdf", "plain.pdf"]
        out = sorted(names, key=a.natural_key)  # must not raise
        self.assertLess(out.index("file2.pdf"), out.index("file10.pdf"))

    def test_zip_cap_is_configurable(self):
        a = load_app(ZIP_MAX_MB=1)
        with self.assertRaises(ValueError):
            a.safe_zip(Uploads.zipped({"big.html": "0" * (2 * 2**20)}))  # compresses to a few KB, declares 2 MB

    def test_merge_html_memory_cap(self):
        a = load_app(MAX_HTML_MB=1)
        c = a.app.test_client()
        r = c.post("/api/merge-html", data={"files": [(io.BytesIO(b"<p>" + b"x" * 700_000), "a.html"), (io.BytesIO(b"<p>" + b"x" * 700_000), "b.html")]},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("limit 1 MB", r.get_json()["error"])
        ok = c.post("/api/merge-html", data={"files": (io.BytesIO(b"<p>hi"), "a.html")}, content_type="multipart/form-data")
        self.assertEqual(ok.status_code, 200)

    def test_zip_symlink_entries_do_not_become_links(self):
        """HTML to PDF unpacks ZIPs; a symlink inside one must not turn into a link to a local file."""
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            info = zipfile.ZipInfo("evil.html"); info.create_system = 3; info.external_attr = 0o120777 << 16
            z.writestr(info, "/etc/passwd")
        out = tempfile.mkdtemp()
        zipfile.ZipFile(io.BytesIO(b.getvalue())).extractall(out)
        path = os.path.join(out, "evil.html")
        self.assertFalse(os.path.islink(path))
        with open(path) as fh:
            self.assertEqual(fh.read(), "/etc/passwd")  # just text

    def test_split_cannot_build_a_giant_file(self):
        a = load_app(MAX_PAGES=100)
        class Doc:
            needs_pass = False
            def __len__(self): return 100
            def select(self, order): raise AssertionError("should have been refused before building")
        with mock.patch.object(a.fitz, "open", return_value=Doc()):
            r = a.app.test_client().post("/api/split-pdf", data={"file": (io.BytesIO(b"%PDF-x"), "a.pdf"), "pages": ",".join(["1-100"] * 50)},
                                         content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("more than 100 pages", r.get_json()["error"])


class HungJobs(unittest.TestCase):
    def test_hung_job_times_out_and_frees_its_slot(self):
        import threading, time
        a = load_app(JOB_SECONDS=1)
        recycled = []
        a._recycle_worker = lambda: recycled.append(True)  # in production this restarts the gunicorn worker
        release = threading.Event()

        @a.app.route("/hang", methods=["POST"])
        @a.limited
        def hang():
            release.wait(10)  # stands in for a parser stuck in an endless loop
            return "late"

        start = time.monotonic()
        r = a.app.test_client().post("/hang")
        release.set()
        self.assertEqual(r.status_code, 504)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(recycled, [True])
        self.assertEqual(a._inflight, {})  # the visitor's counter is released ...
        got = [a.SLOTS.acquire(blocking=False) for _ in range(2)]  # ... and so are the server slots
        self.assertTrue(all(got))
        for _ in got: a.SLOTS.release()

    def test_normal_job_result_and_errors_pass_through(self):
        a = load_app()

        @a.app.route("/ok", methods=["POST"])
        @a.limited
        def ok():
            return "fine"

        @a.app.route("/abort", methods=["POST"])
        @a.limited
        def stop():
            a.abort(418)

        c = a.app.test_client()
        self.assertEqual(c.post("/ok").get_data(as_text=True), "fine")
        self.assertEqual(c.post("/abort").status_code, 418)


if __name__ == "__main__":
    unittest.main()
