# Papertus — image unique : Python 3.12, le code et les papiers d'exemple.
# Aucune ressource réseau au démarrage ; seul le point d'accès LLM est joint à l'exécution.
# Image épinglée (python:3.12-slim, Debian bookworm, 2026-10) : la construction ne touche le réseau que pour pip.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PAPERS_DIR=/papers \
    RUNTIME_DIR=/app/runtime

WORKDIR /app

# Dépendances d'abord (cache Docker), code et données ensuite.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src /app/src
COPY data /app/data

RUN mkdir -p /papers /app/runtime

EXPOSE 8640

CMD ["uvicorn", "papertus.web:app", "--host", "0.0.0.0", "--port", "8640"]
