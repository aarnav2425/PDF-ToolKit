FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright HOME=/tmp

# LibreOffice (Word to PDF) + fonts so documents render with sensible typefaces
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer fonts-liberation fonts-dejavu-core curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright

COPY . .
RUN useradd --system --uid 10001 app && mkdir -p /app/workspace && chown app /app/workspace
USER app

ENV BIND=0.0.0.0:8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
