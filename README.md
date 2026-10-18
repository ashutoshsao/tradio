# Tradio

Telegram alerts when a watched NSE stock's daily SMA8 moves above both SMA13 and SMA21 (bullish) or below both (bearish): provisionally during market hours, then confirmed at the close. The design is in [docs/v1-spec.md](docs/v1-spec.md) and the intraday changes in [docs/v2-spec.md](docs/v2-spec.md).

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
cp .env.example .env
docker compose up -d --wait   # Postgres, plus a tradio_test database for tests
uv run tradio db upgrade      # apply migrations
uv run pytest
```

## Running

```sh
uv run tradio backfill               # once: 2 years of NSE prices
caffeinate -i uv run tradio bot      # bot + intraday checks (market hours) + evening run
uv run tradio intraday --dry-run     # during market hours: each watched stock's live state, nothing sent
```

`UPSTOX_ANALYTICS_TOKEN` in `.env` turns on intraday alerts; without it only the evening run works.

## Migrations

After changing `src/tradio/models.py`:

```sh
uv run alembic revision --autogenerate -m "describe the change"
uv run tradio db upgrade
```

`tests/test_migrations.py` fails if the models and migrations drift apart.
