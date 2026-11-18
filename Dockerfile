# Hermes Agent replica - one container: web dashboard + OpenAI-compatible API + scheduled jobs
# + messaging platforms, all on $PORT. Works on Render, Fly.io, Railway, Docker and any VPS.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HERMES_HOME=/data/hermes HERMES_CWD=/data/workspace PORT=8080
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 hermes

WORKDIR /app
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY hermes ./hermes
# Extras baked into the image; the anthropic SDK lets you pick the native Claude provider.
ARG EXTRAS="[anthropic]"
RUN pip install --no-cache-dir ".${EXTRAS}"
COPY deploy/docker-entrypoint.sh /usr/local/bin/hermes-entrypoint
RUN chmod 755 /usr/local/bin/hermes-entrypoint && mkdir -p /data && chown hermes:hermes /data

# Persistent state (config, memory, skills, sessions, cron) lives under /data - mount a volume there.
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8080'), timeout=4)" || exit 1
ENTRYPOINT ["hermes-entrypoint"]
CMD ["hermes", "serve", "--gateway", "--host", "0.0.0.0"]
