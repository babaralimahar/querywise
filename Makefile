# On Windows without make, use the same targets in tasks.ps1:  .\tasks.ps1 check
.PHONY: install up down db test test-unit check fmt migrate run load goldset eval-zero-shot eval-linked eval-fewshot eval eval-test freeze key

install:
	uv sync

up:
	docker compose up -d --build

down:
	docker compose down

db:
	docker compose up -d --wait db

test:
	uv run pytest -q

test-unit:
	uv run pytest -q tests/unit

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src tests
	uv run pytest -q

fmt:
	uv run ruff check --fix .
	uv run ruff format .

migrate:
	uv run alembic upgrade head

run:
	uv run uvicorn querywise.main:create_app --factory --reload --port 8007

load:
	uv run python -m querywise.data.load

goldset:
	uv run python -m querywise.evals.build

eval-zero-shot:
	uv run python -m querywise.evals --config zero_shot --split dev

eval-linked:
	uv run python -m querywise.evals --config linked --split dev

eval-fewshot:
	uv run python -m querywise.evals --config linked_fewshot --split dev

SET ?= dev
SUITE ?= toy
eval:
	uv run python -m app.evals --suite $(SUITE) --split $(SET)

eval-test:
	uv run python -m app.evals --suite $(SUITE) --split test

freeze:
	uv run python -c "from pathlib import Path; from app.evals.dataset import write_sums; print(write_sums(Path('evals/datasets/$(SUITE)')))"

key:
	uv run python -m app.cli create-key --name $(NAME) --role $(ROLE)
