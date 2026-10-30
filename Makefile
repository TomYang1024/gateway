.PHONY: install dev test unit lint format stop finish-acceptance acceptance-k3

install:
	uv sync --frozen --all-packages

dev: install
	uv run --frozen python scripts/dev_setup.py
	docker compose up -d --build

test:
	docker compose --profile test run --build --rm tests

unit:
	uv run --frozen pytest tests/unit tests/adapter_contract -q

lint:
	uv run --frozen ruff check src sdk migrations scripts tests
	uv run --frozen ruff format --check src sdk migrations scripts tests

format:
	uv run --frozen ruff format src sdk migrations scripts tests

stop:
	docker compose stop

finish-acceptance:
	uv run --frozen python scripts/finish_acceptance.py

acceptance-k3:
	docker compose up -d --wait postgres redis
	sleep 60
	uv run --frozen python scripts/official_acceptance.py --authorized-budget-cny 5 --vendors kimi --kimi-model kimi-k3 --cases tool_stream --min-interval-s 30
	sleep 60
	uv run --frozen python scripts/official_acceptance.py --authorized-budget-cny 5 --vendors kimi --kimi-model kimi-k3.1-pro --min-interval-s 30
