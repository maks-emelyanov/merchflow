# Contributing

## Local development

Install Python 3.14 and [uv](https://docs.astral.sh/uv/), then run:

```bash
uv sync --extra dev --locked
cp .env.example .env
uv run merch fixture
```

The example configuration uses fake providers and dry-run publishing. Do not use live provider credentials or marketplace publishing for tests. Generated artwork, reports, local databases, and `.env` files stay outside Git.

Before opening a pull request, run:

```bash
uv run ruff check .
uv run mypy src
uv run pytest
MERCH_ENV_FILE=.env.example docker compose config --quiet
```

Changes to workflows should include tests for the affected behavior. Describe the behavior and any manual checks in the pull request. Keep API keys, shop IDs, customer data, and production artifacts out of commits, logs, screenshots, and issue reports.

For publication changes, cover both the first attempt and retries after a saved checkpoint. Gallery tests should use synthetic image bytes and mocked provider/CDN responses so they exercise image-content comparisons without downloading live listing photos. Keep Etsy-specific requirements scoped to Etsy when changing shared storefront verification.

The Temporal and browser integration tests are opt-in locally. Their commands and browser setup are in the [README verification section](README.md#verification); CI runs both groups on every push and pull request.

This project uses the [MIT License](LICENSE). Submit only contributions you have the right to offer under that license.
