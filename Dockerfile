FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && python -m venv /opt/venv \
    && chown -R appuser:appuser /opt/venv

COPY agw-root.crt /usr/local/share/ca-certificates/agw-root.crt
RUN chmod 644 /usr/local/share/ca-certificates/agw-root.crt \
    && update-ca-certificates

COPY entrypoint.sh /usr/local/bin/geap-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/geap-entrypoint.sh \
    && chmod 0755 /usr/local/bin/geap-entrypoint.sh

ENV GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    GOOGLE_API_USE_CLIENT_CERTIFICATE=false \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY --chown=appuser:appuser gcp_jumphost_ops_agent/ /app/gcp_jumphost_ops_agent/
COPY --chown=appuser:appuser main.py /app/main.py
COPY --chown=appuser:appuser requirements.txt /app/requirements.txt

USER 10001:10001

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

ENV PORT=8080 \
    GOOGLE_GENAI_USE_VERTEXAI=TRUE \
    GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true \
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true \
    ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=true \
    AGENT_CARD_PATH=/app/gcp_jumphost_ops_agent/agent.json \
    DEMO_TARGET_PROJECT=project-f625f262-ce1a-467a-a2c \
    DEMO_TARGET_ZONE=us-east4-a

CMD ["/usr/local/bin/geap-entrypoint.sh"]
