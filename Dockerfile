# Minimal image for one flaggator process.
#
# The process is its own HTTP server (stdlib WSGI, 32 in-flight requests
# after the body is read, 30 second deadline from accept). gunicorn and uvicorn are not used: gunicorn
# would replace that server, and uvicorn speaks ASGI.
#
# Migrations are found by walking up from the installed source, so the
# install stays editable and /app/db/migrations remains on that walk.

# python:3.12-slim-bookworm
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

RUN useradd --create-home --uid 1000 --user-group flaggator

WORKDIR /app

COPY requirements.txt pyproject.toml README.md LICENSE ./
COPY src ./src
COPY db ./db
COPY ui ./ui

RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
 && pip install --no-cache-dir --no-deps --no-build-isolation -e .

RUN mkdir -p /data && chown flaggator:flaggator /data

USER flaggator

ENV PYTHONUNBUFFERED=1 \
    FLAGGATOR_BIND_HOST=0.0.0.0 \
    FLAGGATOR_ALLOW_CLEARTEXT_BIND=true \
    FLAGGATOR_BIND_PORT=8080 \
    FLAGGATOR_SQLITE_PATH=/data/flaggator.db

EXPOSE 8080
VOLUME /data

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"

CMD ["python", "-m", "main"]
