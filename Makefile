SHELL := /bin/bash
PYTHON := .venv/bin/python
PIP := .venv/bin/pip
BROKER_HOST ?= localhost
BROKER_PORT ?= 18831
STORE ?= sqlite:///homelink.db

.PHONY: install lint test-py test-ts test-e2e broker cloud device api dashboard demo bench analytics clean

install:
	python3.11 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e "python/[dev]"
	cd api && npm install
	cd dashboard && npm install

lint:
	$(PYTHON) -m ruff check python
	cd api && npm run lint --if-present
	cd dashboard && npm run lint --if-present

test-py:
	$(PYTHON) -m pytest python/tests -q

test-ts:
	cd api && npm run build && npm test
	cd dashboard && npm run build && npm test

test-e2e:
	cd dashboard && npx playwright test

broker:
	$(PYTHON) -m homelink.broker --port $(BROKER_PORT)

cloud:
	$(PYTHON) -m homelink.cloud --broker $(BROKER_HOST):$(BROKER_PORT) --store $(STORE)

device:
	$(PYTHON) -m homelink.device --thing vac-001 --broker $(BROKER_HOST):$(BROKER_PORT) --fault none

api:
	cd api && npm run dev

dashboard:
	cd dashboard && npm run dev

demo:
	$(PYTHON) scripts/run_demo.py

bench:
	$(PYTHON) scripts/bench.py

analytics:
	$(PYTHON) -m homelink.analytics.generate_fleet --seed 7 --devices 40 --days 30
	$(PYTHON) -m homelink.analytics.fleet

clean:
	rm -rf .venv api/node_modules dashboard/node_modules api/dist dashboard/dist
	rm -rf .pytest_cache python/.pytest_cache python/.ruff_cache
	rm -rf homelink.db device_spool analytics/out/* bench/out/*
