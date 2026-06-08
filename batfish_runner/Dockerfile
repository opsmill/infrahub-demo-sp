FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app.py is copied for self-contained builds; at runtime docker-compose also
# bind-mounts ./batfish_runner over /app and ./checks (read-only) at
# /app/checks so the engine in checks/batfish_helpers.py is importable as the
# `checks` package without duplicating it here.
COPY . .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=5 \
  CMD curl --fail http://localhost:8080/health || exit 1

CMD ["python", "app.py"]
