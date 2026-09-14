.PHONY: install demo test check build
install:
	uv sync --locked

demo:
	uv run career-radar demo --output .demo

test:
	uv run pytest --cov=career_radar --cov-branch --cov-report=term-missing --cov-report=json:coverage.json
	uv run python scripts/check_core_coverage.py

check: test
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy --strict src/career_radar
	uv run bandit -q -r src/career_radar
	uv run pip-audit
	uv run python scripts/verify_public_repo.py
	uv run python scripts/estimate_actions_budget.py

build:
	uv build
