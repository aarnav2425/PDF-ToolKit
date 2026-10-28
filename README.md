# Personal Tools – Web App
Web version of the scripts in aarnav2425/Personal-Tools-: merge PDFs, merge HTML (files/ZIPs), HTML→PDF, compress PDF, page extraction, PDF→Word and Word→PDF.

```bash
pip install -r requirements.txt
python -m playwright install chromium   # needed only for HTML→PDF
# Word→PDF also needs LibreOffice installed (soffice on PATH)
python app.py                            # open http://localhost:5000
```
Files are processed in memory, or in short-lived temp folders inside `./workspace/` (HTML→PDF only), and deleted after each job.
Current disk use of `./workspace/` is shown at the bottom of the page. It is also emptied on every start.
