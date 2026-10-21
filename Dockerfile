FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    COACH_DB=/data/coach.db \
    COACH_ENV_FILE=/data/.env \
    PORT=8731

RUN useradd --create-home --uid 10001 coach && mkdir /data && chown coach /data
WORKDIR /app

COPY requirements.txt .
# pytest is a dev dependency; leave it out of the image.
RUN grep -v '^pytest' requirements.txt > /tmp/req.txt && pip install --no-cache-dir -r /tmp/req.txt

COPY coach ./coach
COPY bridge ./bridge
COPY migrate ./migrate

USER coach
VOLUME /data
EXPOSE 8731

# Shell form so $PORT expands. Set COACH_SECRET in the environment for a stable cookie key.
CMD uvicorn coach.web.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'
