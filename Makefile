.PHONY: install test lint fmt run docker-build up smoke live-test schema requirements i18n demo

install:
	uv sync --all-extras

test:
	uv run pytest -q

lint:
	uv run ruff check src tests contrib scripts
	uv run ruff format --check src tests contrib scripts
	uv run python scripts/i18n.py check de

# add new interface texts to the German catalogue (then translate the empty entries)
i18n:
	uv run python scripts/i18n.py update de

# a demo archive with fictional documents in /tmp/heftig-demo, then Heftig on it
demo:
	test -d /tmp/heftig-demo || uv run python scripts/demo_archive.py /tmp/heftig-demo
	HEFTIG_ARCHIVE_DIR=/tmp/heftig-demo uv run heftig run

fmt:
	uv run ruff format src tests contrib scripts
	uv run ruff check --fix src tests contrib scripts

run:
	uv run heftig run

docker-build:
	docker build -t heftig:latest .

up:
	docker compose up -d

smoke:
	uv run python scripts/smoke.py

# costs real money: uses the AI provider configured in .env (small synthetic corpus)
live-test:
	uv run python scripts/live_provider_test.py --max-usd 2

schema:
	HEFTIG_ARCHIVE_DIR=$$(mktemp -d) uv run heftig schema > docs/metadata.schema.json

# hash-pinned dependencies for the container image (after changing uv.lock)
requirements:
	uv export --frozen --no-dev --extra anthropic --extra mcp --no-emit-project --format requirements-txt --no-header -o requirements.lock
	sed -i '1i # Exact, hash-pinned dependencies for the container image, generated from uv.lock:\n#   make requirements   (uv export --frozen --no-dev --extra anthropic --extra mcp ...)' requirements.lock
