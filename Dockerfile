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

# Definitive production spine is run_orchestrator.py. Both services set an
# EXPLICIT Railway Start Command:
#   * GTM (acquisition)       -> railway.json startCommand (run_orchestrator.py ...)
#   * GTM Approved Sync       -> service-level override: python -u run_approved.py
# The image default is a SAFE, zero-network preflight (never a real run and never
# the retired run_daily.py), so a service that somehow loses its Start Command
# performs a harmless readiness check instead of silently running acquisition.
CMD ["python", "-u", "run_orchestrator.py", "--preflight-only"]
