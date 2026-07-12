FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p \
    /app/data/raw \
    /app/data/filtered \
    /app/data/enriched \
    /app/data/state \
    /app/logs/runs

# The Daily Pipeline service can use this default.
# The Approved Sync service overrides it in Railway Settings.
CMD ["python", "-u", "run_daily.py"]
