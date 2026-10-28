FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY verifier /app/verifier
RUN pip install --no-cache-dir "scrapling[fetchers]==0.4.15" "apify>=3,<5" "openpyxl==3.1.5" "tldextract==5.3.2" "dnspython==2.8.0"
RUN python -m playwright install --with-deps chromium
RUN python -m patchright install chromium

CMD ["python", "-m", "verifier.actor"]
