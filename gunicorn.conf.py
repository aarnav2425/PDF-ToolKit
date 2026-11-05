"""Production server settings. Start with:  gunicorn -c gunicorn.conf.py app:app"""
import os, shutil

os.environ.setdefault("RECYCLE_ON_HANG", "1")  # a hung job makes its worker restart (see app.py, run_with_deadline)
bind = os.environ.get("BIND", "127.0.0.1:8000")
workers = int(os.environ.get("WEB_CONCURRENCY", 2))   # separate processes: PDF work isn't stuck behind the GIL
threads = int(os.environ.get("THREADS", 4))            # light requests and waiting-for-a-slot requests
worker_class = "gthread"
timeout = 300            # must exceed the slowest conversion (PDF to Word: 180 s)
graceful_timeout = 30
max_requests = 200       # recycle workers now and then: PyMuPDF / LibreOffice wrappers can leak memory
max_requests_jitter = 50
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None
# Request logs hold IP addresses (needed to investigate abuse). ACCESS_LOG=0 switches them off.
accesslog = "-" if os.environ.get("ACCESS_LOG", "1") == "1" else None
errorlog = "-"
limit_request_line = 4094        # refuse oversized request lines / headers early
limit_request_fields = 50
limit_request_field_size = 8190


def on_starting(server):
    """Runs once in the master process: clear leftovers from a previous crash. (app.py must not do this
    itself, or every worker that starts would delete the running jobs of the others.)"""
    ws = os.environ.get("WORKSPACE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")
    os.makedirs(ws, exist_ok=True)
    for n in os.listdir(ws):
        p = os.path.join(ws, n)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass
