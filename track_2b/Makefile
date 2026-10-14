# Paperasse — Track 2B. `make run` est ce que lancent les juges (Docker, dépôt propre).
.PHONY: run data eval test dev stop seed

# Interpréteur pour les cibles locales : le venv du dépôt s'il existe, sinon python3.
PY ?= $(shell test -x ../.venv/bin/python && echo ../.venv/bin/python || echo python3)
export PYTHONPATH := src

# Démarre l'application dans Docker (image construite à la volée) sur http://localhost:8640
run:
	docker compose up --build

stop:
	docker compose down

# Régénère les 100 papiers synthétiques et leur vérité (déterministe, graine 42)
data:
	$(PY) -m paperasse.generate --out data --seed 42

# Compare le modèle seul (baseline) à notre chaîne vérifiée ; écrit docs/results.md + results.json
eval:
	$(PY) -m paperasse.eval --data data --out docs

# Tests unitaires ; les tests qui appellent le modèle sont sautés sans LLM_API_KEY
test:
	$(PY) -m pytest -q tests

# Serveur de développement avec rechargement, sans Docker (lit ./papers ou PAPERS_DIR)
dev:
	PAPERS_DIR=$${PAPERS_DIR:-./papers} $(PY) -m uvicorn paperasse.web:app --reload --port 8640 --app-dir src

# Livre les lectures déjà faites avec les papiers d'exemple : `make run` les montre alors
# tout de suite, sans relire les 100 papiers (chaque autre papier est lu normalement).
seed:
	cp runtime/cache.json data/cache.json
