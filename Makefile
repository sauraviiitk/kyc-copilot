# KYC Copilot — developer entrypoints.
# Windows: `make` may be unavailable; equivalent commands are in README.md
# and ./tasks.ps1.

PY ?= python
BACKEND := backend
FRONTEND := frontend

.PHONY: help install dev dev-backend dev-frontend test test-backend test-frontend lint eval eval-report e2e docker-up docker-down migrate seed

help:
	@echo "make install       - install backend + frontend dependencies"
	@echo "make dev           - run backend (:8000) and frontend (:5173)"
	@echo "make test          - run backend + frontend test suites"
	@echo "make lint          - ruff + tsc"
	@echo "make eval          - run the evaluation harness for prompt v1 and v2"
	@echo "make migrate       - run alembic migrations"
	@echo "make seed          - seed policies + evaluation scenarios"
	@echo "make docker-up     - docker compose up --build"

install:
	$(PY) -m pip install -r $(BACKEND)/requirements-dev.txt
	cd $(FRONTEND) && npm install

dev:
	@echo "Run 'make dev-backend' and 'make dev-frontend' in two terminals."

dev-backend:
	cd $(BACKEND) && $(PY) -m uvicorn app.main:app --reload --port 8000

dev-frontend:
	cd $(FRONTEND) && npm run dev

test: test-backend test-frontend

test-backend:
	cd $(BACKEND) && $(PY) -m pytest -q

test-frontend:
	cd $(FRONTEND) && npm run test -- --run

lint:
	cd $(BACKEND) && $(PY) -m ruff check app tests
	cd $(FRONTEND) && npm run lint

eval:
	cd $(BACKEND) && $(PY) -m app.evaluation.cli run --versions v1,v2

eval-report:
	cd $(BACKEND) && $(PY) -m app.evaluation.cli report

migrate:
	cd $(BACKEND) && $(PY) -m alembic upgrade head

seed:
	cd $(BACKEND) && $(PY) -m app.db.seed

e2e:
	cd $(FRONTEND) && npx playwright test

docker-up:
	docker compose up --build

docker-down:
	docker compose down -v
