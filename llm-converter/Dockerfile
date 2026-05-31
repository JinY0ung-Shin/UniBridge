FROM python:3.12-slim

# Optional mirror/proxy overrides — leave empty for default (public) endpoints
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""

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

EXPOSE 4001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4001"]
