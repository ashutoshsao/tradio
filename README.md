# Tradio

Telegram alerts when a watched NSE stock's SMA8 crosses above both SMA13 and SMA21 (bullish) or below both (bearish). During market hours, signals use completed **15-minute candles**, checked five seconds after each candle closes. Separate **daily** signals use NSE's official daily closes. See [the current v3 specification](docs/v3-spec.md); [v1](docs/v1-spec.md) and [v2](docs/v2-spec.md) document earlier behaviour.

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
uv run tradio bot                    # local development only; never alongside the cluster bot
uv run tradio intraday --dry-run     # completed 15-minute candle states; nothing stored or sent
```

`UPSTOX_ANALYTICS_TOKEN` in `.env` turns on intraday alerts; without it only the evening run works.

## Migrations

After changing `src/tradio/models.py`:

```sh
uv run alembic revision --autogenerate -m "describe the change"
uv run tradio db upgrade
```

`tests/test_migrations.py` fails if the models and migrations drift apart.

## Production

The bot runs in Kubernetes (`tradio-app`, deployment `bot`), with one replica and `Recreate`. The scheduler runs inside the pod; progress survives restarts in Postgres. Images and migration Jobs are managed in `~/projects/ops/tradio-app-k8s/`. Apply the database migration before rolling out the new bot image. See [v3 deployment and rollback notes](docs/v3-spec.md#commands-and-deployment).
