FROM python:3.12-slim

# Optional mirror overrides — leave empty for default (public) endpoints
ARG APT_DEBIAN_MIRROR=""
ARG MS_PACKAGES_URL="https://packages.microsoft.com"
ARG MS_SKIP_GPG=""
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""

RUN if [ -n "$APT_DEBIAN_MIRROR" ]; then \
      sed -i "s|http://deb.debian.org|$APT_DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi

# Install ODBC driver for MSSQL
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl gnupg2 unixodbc-dev && \
    if [ -n "$MS_SKIP_GPG" ]; then \
      echo "deb [trusted=yes] ${MS_PACKAGES_URL}/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list; \
    else \
      curl -fsSL "${MS_PACKAGES_URL}/keys/microsoft.asc" \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg && \
      echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] ${MS_PACKAGES_URL}/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list; \
    fi && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y msodbcsql18 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN set -eu; \
    PIP_ARGS="--no-cache-dir"; \
    if [ -n "$PIP_INDEX_URL" ]; then \
      PIP_ARGS="$PIP_ARGS --index-url $PIP_INDEX_URL"; \
      [ -n "$PIP_TRUSTED_HOST" ] && PIP_ARGS="$PIP_ARGS --trusted-host $PIP_TRUSTED_HOST"; \
    else \
      PIP_ARGS="$PIP_ARGS --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org"; \
    fi; \
    pip install $PIP_ARGS -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
RUN mkdir -p /app/data && chown -R 1000:1000 /app

EXPOSE 8000
USER 1000:1000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
