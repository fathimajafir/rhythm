.PHONY: install test run demo mcp-test mcp-http mcp-stdio prod-api prod-mcp backup clean

install:
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt pytest httpx

test:
	.venv/bin/python -m pytest

run:
	PYTHONPATH=src .venv/bin/uvicorn rhythm.api:app --reload --port 8099

demo:
	PYTHONPATH=src .venv/bin/python scripts/demo.py

mcp-test:
	cd mcp && ../.venv/bin/python -m pytest -q

mcp-http:
	PYTHONPATH=src:mcp/src .venv/bin/python -m rhythm_mcp --transport http --port 8090

mcp-stdio:
	PYTHONPATH=src:mcp/src .venv/bin/python -m rhythm_mcp --transport stdio

# --- production (no Docker): systemd units live in deploy/ ------------------
# Foreground runs for ad-hoc verification. Real services: see docs/deploy/production.md
prod-api:
	@test -f deploy/rhythm.env || { echo "copy deploy/rhythm.env.example to deploy/rhythm.env first"; exit 1; }
	PYTHONPATH=src .venv/bin/uvicorn rhythm.api:app --host 127.0.0.1 --port 8099 --proxy-headers --no-access-log &
	@sleep 1 && curl -sf http://127.0.0.1:8099/healthz && echo " <- api up (kill %1 to stop)"

prod-mcp:
	@test -f deploy/rhythm.env || { echo "copy deploy/rhythm.env.example to deploy/rhythm.env first"; exit 1; }
	RHYTHM_API_URL=http://127.0.0.1:8099 PYTHONPATH=src:mcp/src .venv/bin/python -m rhythm_mcp --transport http --host 127.0.0.1 --port 8090

backup:
	./deploy/backup.sh

clean:
	rm -rf .venv .pytest_cache **/__pycache__ mcp/**/__pycache__
