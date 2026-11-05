# Personal Tools

A self-hosted web app for everyday document jobs: merge PDFs, merge HTML, convert HTML, Word and PDF files in both directions, compress PDFs, rearrange pages visually, pull out pages, add watermarks and page numbers, and add or remove PDF passwords. Everything runs on your own computer. Files are processed in memory or in short-lived temporary folders and deleted after each job.

It grew out of a handful of small command-line scripts (merge, compress, share, output) and now wraps them in a browser interface with proper input checks, limits and sandboxing.

---

## Contents

- [Features](#features)
- [Quick start](#quick-start)
- [Using the tools](#using-the-tools)
- [Configuration](#configuration)
- [Sharing with friends](#sharing-with-friends)
- [Security model](#security-model)
- [Architecture](#architecture)
- [Project structure](#project-structure)
- [API reference](#api-reference)
- [Limits](#limits)
- [Troubleshooting](#troubleshooting)
- [Deployment notes](#deployment-notes)
- [Roadmap](#roadmap)
- [Contributing](#contributing)

---

## Features

| Tool | What it does | Engine |
|---|---|---|
| **Merge PDFs** | Combine PDFs. Reorder by dragging, with arrows, or sort by filename (`1, 2, 10`). | pypdf |
| **Merge HTML** | Join `.html` / `.htm` files and ZIPs of them into one page. Sort by name, by `_YYYYMMDD` date in the name, or manually. Output is one clean document or plain joined text. | built in |
| **HTML to PDF** | Render a page, or a ZIP with its images and styles, to an A4 PDF. Several files are merged first. | Chromium via Playwright |
| **PDF to Word** | Convert a PDF to an editable `.docx`. Works best on PDFs with real text. | pdf2docx |
| **Word to PDF** | Convert `.docx`, `.doc`, `.odt` or `.rtf` to PDF. | LibreOffice |
| **Compress PDF** | Two methods: *Small* redraws pages as JPEG with adjustable DPI and quality; *Lossless* keeps text and vectors. Shows how much smaller the result is. Several files at once return a ZIP. | PyMuPDF |
| **Organize pages** | Page thumbnails. Drag to reorder, rotate, duplicate or delete pages, then save. | PyMuPDF |
| **Extract pages** | Keep only chosen pages in the order given, such as `1-3, 5, 8-10`. | PyMuPDF |
| **Watermark** | Diagonal or horizontal text watermark on every page, with colour and strength. | PyMuPDF |
| **Page numbers** | Number every page: `1`, `Page 1`, `Page 1 of 10` or `1 / 10`, in any corner or edge centre. | PyMuPDF |
| **Protect PDF** | Add a password (AES-256). | PyMuPDF |
| **Unlock PDF** | Remove the password from a PDF, when you know it. | PyMuPDF |

Also included:

- A modern, responsive interface: light and dark themes, keyboard-friendly controls, and an iPhone-ready layout (safe-area aware, large touch targets, a tool picker that slides up from the bottom, and the main button pinned within reach).
- Real upload percentages while files are sent.
- **Batch jobs:** drop several files into *Compress* or *Word to PDF* and get one ZIP back (a `_errors.txt` inside lists any file that failed).
- **Preview** of the first pages of a PDF result before you download it.
- **Recent results** (off by default): keep your last 8 results in this browser, and download them again without redoing the job. They never go to the server, and switching the option off deletes them.
- **Install on your phone:** see [Install as an app](#install-as-an-app).
- A live readout of the temporary workspace size in the page footer (local use only).
- An optional login for when you share the app.
- A hardened server: see [Security model](#security-model).

---

## Quick start

**Requirements**

- Python 3.9 or newer (3.10+ recommended, see [Troubleshooting](#troubleshooting))
- LibreOffice, only for **Word to PDF**
- About 400 MB of disk space for the Chromium download, only for **HTML to PDF**

Tested on macOS (Python 3.9) and Linux (Python 3.12). Windows has not been tested.

```bash
git clone https://github.com/aarnav2425/Personal-Tools-.git
cd Personal-Tools-

python3 -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate

python3 -m pip install -r requirements.txt
python3 -m playwright install chromium    # for HTML to PDF
```

Install LibreOffice for Word to PDF:

```bash
brew install --cask libreoffice           # macOS
sudo apt install libreoffice-writer       # Debian / Ubuntu
# Windows: download the installer from libreoffice.org
```

Run it:

```bash
python3 app.py
```

Open <http://127.0.0.1:5000>. Stop the server with `CTRL+C`.

> The app listens on `127.0.0.1` only, so nothing else on your network can reach it unless you choose to share it.

---

## Using the tools

1. Pick a tool in the left rail (a row of chips on phones).
2. Drop files onto the sheet, or click it to choose files.
3. Adjust options if the tool has any.
4. Press the action button, then download the result.

| Tool | Options |
|---|---|
| Merge PDFs | **Order of files:** as listed, or by filename |
| Merge HTML | **Order:** by filename, by date in name, or as listed. **Result:** one clean HTML document, or plain joined text |
| HTML to PDF | **Order** (only matters when several files are given) |
| Compress PDF | **Method:** small or lossless. **Detail:** 36 to 300 DPI. **Image quality:** 1 to 95 |
| Organize pages | none; use the buttons on each page (rotate left or right, duplicate, move earlier or later, delete) or drag pages |
| Extract pages | **Pages to keep**, e.g. `1-3, 5, 8-10` |
| Watermark | **Text** (Latin letters), **Direction**, **Colour**, **Strength** 5 to 100 |
| Page numbers | **Style**, **Position**, **First page number**, **Size** 8 to 24 pt |
| Protect PDF | **Password** (4 to 128 characters) |
| Unlock PDF | **Current password** |
| PDF to Word, Word to PDF | none |

**Notes on results**

- *Compress, Small mode* turns every page into an image, so text can no longer be selected. If the result is not smaller than the original, the page says so and suggests the other method.
- *HTML to PDF* runs with JavaScript off for safety. Pages that build their content with scripts will come out incomplete.
- *PDF to Word* does no text recognition (OCR). Scanned pages stay as pictures.
- *Protect PDF* cannot be undone without the password. There is no recovery.
- *Organize pages* shows up to 200 pages (`ORGANIZER_MAX_PAGES`). For longer files, use *Extract pages* first. Page links and bookmarks are kept only if the page stays in the file.
- *Watermark* and *Page numbers* support Latin letters, digits and common symbols only.
- *Merge, compress, organize and the other PDF tools* ask you to unlock a password-protected PDF first.
- *Merge HTML* keeps any `<script>` tags from the inputs. They will run when you open the merged file, so only merge pages you trust.

---

## Configuration

Settings are environment variables, set when you start the app.

| Variable | Default | Purpose |
|---|---|---|
| `APP_PASSWORD` | *(unset)* | When set, every page and API call requires a login. Any username works; the password must match. |
| `APP_PASSWORD_HASH` | *(unset)* | Safer than `APP_PASSWORD`: a hash, so no readable password is stored. Make one with `python3 -c "from werkzeug.security import generate_password_hash as g; import getpass; print(g(getpass.getpass()))"`. |
| `APP_PASSWORD_FILE`, `APP_PASSWORD_HASH_FILE` | *(unset)* | Read the value from a file (Docker secrets) instead of the environment. |
| `APP_USER` | *(unset)* | Optional. With a password, also require this exact username. |
| `LOGIN_MAX_FAILS`, `LOGIN_WINDOW` | 8, 600 | Wrong logins allowed per IP within this many seconds before that IP is locked out. |
| `PER_IP_JOBS` | 2 | Heavy jobs one visitor may run at once. |
| `PUBLIC_MODE` | *(unset)* | Set to `1` on a public server: requires HTTPS, a host name list and the proxy setting, or the app refuses to start. See `DEPLOY.md`. |
| `ALLOWED_HOSTS` | *(unset)* | Extra host names the app answers to when **no** password is set. Comma separated. An entry starting with a dot, such as `.example.com`, matches all subdomains. |

Examples:

```bash
APP_PASSWORD='a-long-passphrase' python3 app.py
ALLOWED_HOSTS=.mytunnel.example python3 app.py
```

More settings (all optional environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `MAX_UPLOAD_MB` | 200 | Largest upload accepted |
| `MAX_PAGES` | 2000 | Page limit for the PDF tools |
| `ORGANIZER_MAX_PAGES` | 200 | Page limit for *Organize pages* (thumbnails are rendered on the server) |
| `BATCH_MAX` | 20 | Files per batch |
| `BATCH_SECONDS` | 170 | Stop starting new files in a batch after this long |
| `JOB_SECONDS` | 300 | Hard limit for one request's heavy work. A job stuck in a parser is stopped (504) and its slot freed; under gunicorn the worker restarts |
| `ZIP_MAX_MB` | 200 | Most a ZIP or `.docx` may unpack to |
| `MAX_HTML_MB` | 60 | Most HTML text *Merge HTML* will hold in memory |
| `MAX_RASTER_MP` | 120 | Megapixels per page when *Compress* redraws pages as images |
| `SLOTS` | 2 | Heavy jobs allowed at once (per worker process) |

For a public server see `DEPLOY.md`.

The port is the last line of `app.py` (`port=5000`). On macOS, port 5000 can be taken by AirPlay Receiver; change it to 5001 if you see "address already in use".

---

## Sharing with friends

By default only your own computer can open the app. To let others use it, put a tunnel in front of it **and set a password**.

```bash
# terminal 1
APP_PASSWORD='a-long-passphrase' python3 app.py

# terminal 2: gives you a public https link
brew install cloudflared
cloudflared tunnel --url http://localhost:5000
```

Send friends the link and the password. Tips:

- Use a long passphrase. There is no lockout after failed attempts.
- Keep your computer awake while sharing (`caffeinate -i` on macOS).
- Tailscale is a good alternative: friends join your private network and the app is never exposed to the open internet.
- Anyone with the password can run conversions on your machine, so share only with people you trust.

---

## Install as an app

On an iPhone, open the site in Safari, tap **Share**, then **Add to Home Screen**. On Android (Chrome) use the menu, then **Install app**. It opens full screen with its own icon. The app still needs the server to be reachable, because the work happens there. Installing needs an `https://` address (a tunnel or a real domain provides one).

Note: with `APP_PASSWORD` set, iOS may ask for the login again each time you open the home-screen app, because it uses basic browser authentication.

---

## Security model

The app opens files from the browser, and some of those files can be hostile. These protections are built in; the main ones are covered by tests (`python3 -m unittest discover -s tests -v`):

| Risk | Protection |
|---|---|
| Reading local files through HTML to PDF (`file://` iframes and images) | Chromium renders with JavaScript off, and all requests are blocked except those to a throwaway local server that serves only the job's folder |
| Requests from a rendered page to internal services | Same network lockdown |
| ZIP bombs (in ZIP uploads and inside `.docx` / `.odt`) | Caps of 1,000 files and 200 MB unpacked |
| Word documents that pull in outside files or carry macros | Pre-scan rejects external links, `INCLUDETEXT` / `INCLUDEPICTURE` fields (also when split across runs or written with character references), linked sections, DDE and macros, however the XML is spelled. Ordinary web hyperlinks are allowed |
| A parser that hangs on a crafted PDF | Each heavy job has a deadline (`JOB_SECONDS`); on timeout the request ends with 504, its server slot is freed and the worker restarts |
| Slow or crashing converters | PDF to Word and Word to PDF run as separate processes with time limits (180 s and 120 s), CPU-time and file-size caps, and no core dumps. Timeouts kill the whole process group so nothing lingers |
| Memory and CPU exhaustion | Upload limit, page limit, pixel limit for rasterizing, and at most 2 heavy jobs at once |
| Other websites using your local app (CSRF) | Cross-site and foreign-origin POSTs are refused |
| DNS rebinding | Only `localhost` and `127.0.0.1` are accepted as host names unless you allow more |
| Information leaks | The disk readout shows size only, never paths. Error messages are generic. Secret-looking environment variables are stripped before running converters |
| Browser-side attacks | Content Security Policy with a per-page nonce (no inline scripts allowed otherwise), `X-Frame-Options`, `nosniff`, `Permissions-Policy`, no caching, HSTS over HTTPS, escaped filenames |
| Password guessing | Per-IP lockout after repeated wrong logins, constant-time checks, optional hashed password |
| Data in logs | Tracebacks and converter output are never logged, since they can quote document text |
| Other users on the same computer | The workspace folder is private (mode 700), and the app refuses to start if it is a symlink |

**What is not covered**

- The Word-document scan is a safety filter, not a guarantee. Keep LibreOffice updated.
- The PDF and Office libraries parse untrusted files, and bugs in them appear from time to time. Keep dependencies current and run `pip-audit -r requirements.txt` regularly.
- The built-in Flask server is for personal use. For anything public, use the Docker setup in `DEPLOY.md` (gunicorn, HTTPS proxy, rate limits). See `SECURITY.md` for the full threat model and checklist.
- The password uses HTTP Basic authentication. Use it only over HTTPS (a tunnel provides that) or on localhost.

Please report security issues privately to the repository owner rather than in a public issue.

---

## Architecture

```mermaid
flowchart LR
    B[Browser<br/>templates/index.html] -->|multipart POST| F[Flask app.py]
    F --> G{Guard<br/>login, host,<br/>origin checks}
    G --> M[PDF and HTML tools<br/>pypdf, PyMuPDF<br/>in-process]
    G --> H[HTML to PDF<br/>local file server +<br/>Chromium, JS off]
    G --> W[pdf_worker.py<br/>pdf2docx<br/>separate process]
    G --> L[LibreOffice<br/>headless<br/>separate process group]
    H & W & L --> T[(workspace/<br/>temporary jobs)]
    M --> R[Result in memory]
    H & W & L --> R
    R --> B
```

- Light tools work entirely in memory and return the result in the same request.
- Heavy tools use a throwaway folder inside `workspace/`, which is deleted when the job ends, whether it succeeds or fails. The whole folder is also emptied each time the app starts.
- A semaphore keeps at most two heavy jobs running at once; extra requests wait up to 10 seconds, then get a "busy" message.

---

## Project structure

```
Personal-Tools-/
├── app.py               Flask server: routes, security checks, limits, cleanup
├── pdf_worker.py        Runs pdf2docx in its own process (timeout + isolation)
├── requirements.txt     Python dependencies
├── README.md            This file
├── .gitignore           Ignores workspace/ and __pycache__/
├── templates/
│   └── index.html       The whole interface: markup, styles and script
├── static/              Home-screen install files: manifest, icons, a do-nothing service worker
└── workspace/           Created at start-up; temporary job folders live here
```

Keep `pdf_worker.py` next to `app.py`, and keep the folder named `templates`.

---

## API reference

Every tool is a plain `POST` that returns the result file, so you can script them without the browser. On errors the response is JSON: `{"error": "..."}`.

| Endpoint | Form fields | Returns |
|---|---|---|
| `POST /api/merge-pdf` | `files` (2 or more), `sort` = `manual` \| `natural` | `merged.pdf` |
| `POST /api/merge-html` | `files` (`.html`, `.htm`, `.zip`), `sort` = `natural` \| `date` \| `manual`, `structure` = `valid` \| `raw` | `merged_output.html` |
| `POST /api/html-to-pdf` | `files`, `sort`, `merge_first` = `1` (optional) | `output.pdf` |
| `POST /api/pdf-to-word` | `file` | `converted.docx` |
| `POST /api/word-to-pdf` | `file` (`.docx`, `.doc`, `.odt`, `.rtf`), or several `files` | `converted.pdf`, or `results.zip` |
| `POST /api/compress-pdf` | `file` (or several `files`, which return `results.zip`), `mode` = `raster` \| `lossless`, `dpi` (36 to 300), `quality` (1 to 95) | `compressed.pdf` |
| `POST /api/split-pdf` | `file`, `pages` such as `1-3,5,8-10` | `extracted.pdf` |
| `POST /api/pdf-thumbs` | `file`, `width` (80 to 600), `limit` (0 = all pages) | JSON `{pages, thumbs:[{w,h,b64}]}` |
| `POST /api/organize-pdf` | `file`, `ops` = JSON list of `[source page index from 0, clockwise degrees]` in the new order | `organized.pdf` |
| `POST /api/watermark-pdf` | `file`, `text`, `angle` = `45` \| `0`, `color` = `gray` \| `red` \| `blue` \| `black`, `opacity` (5 to 100) | `watermarked.pdf` |
| `POST /api/page-numbers` | `file`, `format` = `n` \| `page` \| `of` \| `slash`, `position` = `bl` \| `bc` \| `br` \| `tl` \| `tc` \| `tr`, `start`, `size` | `numbered.pdf` |
| `POST /api/protect-pdf` | `file`, `password` | `protected.pdf` |
| `POST /api/unlock-pdf` | `file`, `password` | `unlocked.pdf` |
| `GET /api/disk-usage` | none | `{"bytes": 0}` |

Examples:

```bash
curl -F files=@a.pdf -F files=@b.pdf -F sort=natural \
     http://127.0.0.1:5000/api/merge-pdf -o merged.pdf

curl -F file=@report.pdf -F mode=raster -F dpi=120 -F quality=60 \
     http://127.0.0.1:5000/api/compress-pdf -o small.pdf

# with a password set
curl -u me:'a-long-passphrase' -F file=@notes.docx \
     http://127.0.0.1:5000/api/word-to-pdf -o notes.pdf
```

Status codes: `400` bad input, `401` login needed, `403` blocked cross-site request, `413` upload too large, `429` server busy, `501` a required program is missing, `504` conversion timed out.

---

## Limits

| Limit | Value |
|---|---|
| Upload size | 200 MB |
| Pages per PDF job | 2,000 |
| Pages in *Organize pages* | 200 |
| Files per batch | 20 (and about 3 minutes) |
| ZIP contents | 1,000 files, 200 MB unpacked |
| Rasterized page size | about 120 million pixels at the chosen DPI |
| Heavy jobs at once | 2 |
| HTML to PDF page load | 30 s |
| PDF to Word | 180 s |
| Word to PDF | 120 s |

Compressing a very long PDF in *Small* mode takes a while, because every page is redrawn.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| PDF to Word says `pdf2docx is not installed` (501) | Install into the same Python you run the app with: `python3 -m pip install -r requirements.txt` |
| PDF to Word fails with `'Rect' object has no attribute 'get_area'` | Old `pdf2docx` with a newer PyMuPDF. The app patches this automatically inside `pdf_worker.py`. If you see it, make sure you are running the latest `pdf_worker.py` |
| Word to PDF says `LibreOffice not found` (501) | Install LibreOffice (see [Quick start](#quick-start)) and restart the app. On macOS the app also checks `/Applications/LibreOffice.app` |
| Word to PDF says the document links to outside files | The safety scan rejected it. Open it in Word, remove linked files or fields and macros, save a copy, and try again |
| HTML to PDF fails on a fresh install | Run `python3 -m playwright install chromium` |
| HTML to PDF output is missing content | The page depends on JavaScript, which is disabled on purpose |
| "Address already in use" on port 5000 | On macOS, turn off AirPlay Receiver in System Settings, or change the port in the last line of `app.py` |
| Page shows 400 when opened through a tunnel | Set `APP_PASSWORD`, or add the tunnel name to `ALLOWED_HOSTS` |
| 429 "Server busy" | Two heavy jobs are already running. Wait a few seconds and retry |
| `pip` installs an older `pdf2docx` on Python 3.9 | Expected: newer releases need Python 3.10+. Upgrading Python (`brew install python@3.12`) removes the need for the compatibility patch |

Server errors are logged in the terminal where you started the app.

---

## Deployment notes

- **Your own computer plus a tunnel** is the simplest way to share: see [Sharing with friends](#sharing-with-friends).
- **Small server:** run it in a container or directly on a VM with at least 2 GB of RAM, with Chromium and LibreOffice installed, behind a reverse proxy with HTTPS and `APP_PASSWORD` set. Use a production WSGI server such as Gunicorn instead of `python3 app.py`.
- **Vercel and similar serverless hosts are a poor fit.** Request bodies are capped at a few megabytes, LibreOffice cannot be installed there, and Chromium is hard to fit in the function size limit.
- **Free hosting tiers** usually have too little memory for Chromium and LibreOffice.

---

## Contributing

Issues and pull requests are welcome. Before opening a pull request:

1. Run the app and try the tool you changed in the browser.
2. Run `bandit -r app.py pdf_worker.py` and `pip-audit -r requirements.txt`.
3. If you add an endpoint, apply the same safeguards as the others: the `@limited` decorator for heavy work, size and page checks, and generic error messages.

