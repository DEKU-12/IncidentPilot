# One image for every role. Cloud Run picks the role with the ROLE env var:
# frontend | orders | payments | mcp | agent
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    INCIDENTPILOT_VAR_DIR=/tmp/var ROLE=frontend PORT=8080
WORKDIR /app

COPY pyproject.toml README.md ./
COPY incidentpilot ./incidentpilot
COPY runbooks ./runbooks
RUN pip install -e .

RUN useradd --create-home app
USER app
CMD ["sh", "-c", "exec incidentpilot serve \"$ROLE\""]
