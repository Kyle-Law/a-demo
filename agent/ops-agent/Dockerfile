# ops-agent image (B14). Built and pushed by .github/workflows/ci.yml to ghcr.io/kyle-law/a-demo-ops-agent.
FROM python:3.13-slim@sha256:81c118faaca296a46b46a7dd7f3bf7575a956d8a4af22623b3de2ff504a3e89b

# kubectl for the read-only kubectl tool; same version as the clusters (infra/ansible).
ADD --chmod=755 --checksum=sha256:33bc88a24c3b09cf55bfd59e6ca977881b61fc3f25ab65bc80c65b430248648e \
    https://dl.k8s.io/release/v1.36.5/bin/linux/amd64/kubectl /usr/local/bin/kubectl

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ops_agent ./ops_agent

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
ENTRYPOINT ["python", "-m", "ops_agent"]
CMD ["run"]
