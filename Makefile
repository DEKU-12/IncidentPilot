PY := .venv/bin/python
IP := $(PY) -m incidentpilot.cli

.PHONY: install test up traffic logs status clear eval-data eval

install:  ## install the package and dev tools into .venv
	$(PY) -m pip install -e ".[dev]"

test:  ## run the test suite
	$(PY) -m pytest -q

up:  ## start ShopDemo (frontend :8001, orders :8002, payments :8003)
	$(IP) up --fresh

traffic:  ## send ~5 req/s until Ctrl+C
	$(IP) traffic --rps 5

logs:  ## follow warnings and errors from every service
	$(IP) logs --severity WARNING -f

status:
	$(IP) chaos status

clear:
	$(IP) chaos clear

eval-data:  ## record 150 incidents with known root causes
	$(IP) eval generate -n 150

eval:  ## score the agent (MODEL=google_vertexai:gemini-2.5-flash to use Gemini)
	@test -d var/evals/dataset || $(IP) eval generate -n 150
	$(IP) eval run --model $(or $(MODEL),baseline)
