VENV ?= .venv
PY := $(VENV)/bin/python

.PHONY: venv lint smoke

venv: ## Create the virtual environment and install the examples' dependencies
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -r requirements.txt ruff

lint: ## Check formatting and lint
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/ruff check .

smoke: ## Run the integration checks against PODIUM_API_BASE
	$(PY) scripts/smoke.py
