FROM alpine/git:2.49.1@sha256:c0280cf9572316299b08544065d3bf35db65043d5e3963982ec50647d2746e26 AS upstream

ARG JOB_BOARDS_COMMIT=da7885cff552c513319318f2f31ed23f049f426e
RUN git clone --filter=blob:none https://github.com/mherzog4/job-boards.git /source/job-boards \
    && git -C /source/job-boards checkout "$JOB_BOARDS_COMMIT" \
    && rm -rf /source/job-boards/.git

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    JOB_BOARDS_PATH=/opt/job-boards \
    PORT=8000

WORKDIR /app

RUN groupadd --system --gid 10001 jobhunt \
    && useradd --system --uid 10001 --gid jobhunt --home-dir /app jobhunt

COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --from=upstream /source/job-boards /opt/job-boards
COPY app ./app
COPY data ./data
COPY n8n ./n8n
COPY README.md LICENSE .

RUN mkdir -p /app/runtime \
    && chown -R jobhunt:jobhunt /app /opt/job-boards

USER jobhunt
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).read()"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
