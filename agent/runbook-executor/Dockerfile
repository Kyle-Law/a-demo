# runbook-executor image (B15). Built and pushed by .github/workflows/ci.yml to
# ghcr.io/kyle-law/a-demo-runbook-executor. No kubectl: it calls the Kubernetes API directly with
# the short-lived tokens Vault mints.
FROM python:3.13-slim@sha256:81c118faaca296a46b46a7dd7f3bf7575a956d8a4af22623b3de2ff504a3e89b

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY runbook_executor ./runbook_executor

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
ENTRYPOINT ["python", "-m", "runbook_executor"]
CMD ["run"]
