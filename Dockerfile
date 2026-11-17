# Hermes Agent replica - gateway / API server image (#72 Docker)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HERMES_HOME=/data/hermes
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 hermes

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY hermes ./hermes
ARG EXTRAS=""
RUN pip install --no-cache-dir ".${EXTRAS}"

USER hermes
VOLUME ["/data/hermes", "/workspace"]
WORKDIR /workspace
# 8650 = messaging webhooks (WhatsApp/Teams/Google Chat), 8642 = OpenAI-compatible API
EXPOSE 8650 8642
HEALTHCHECK --interval=60s --timeout=5s CMD hermes doctor >/dev/null 2>&1 || exit 1
ENTRYPOINT ["hermes"]
CMD ["gateway", "run"]
