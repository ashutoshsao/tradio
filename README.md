# Tradio

Telegram alerts when a watched NSE stock's daily SMA8 moves above both SMA13 and SMA21 (bullish) or below both (bearish). The design is in [docs/v1-spec.md](docs/v1-spec.md).

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
cp .env.example .env
docker compose up -d --wait   # Postgres, plus a tradio_test database for tests
uv run tradio db upgrade      # apply migrations
uv run pytest
```

## Migrations

After changing `src/tradio/models.py`:

```sh
uv run alembic revision --autogenerate -m "describe the change"
uv run tradio db upgrade
```

`tests/test_migrations.py` fails if the models and migrations drift apart.
