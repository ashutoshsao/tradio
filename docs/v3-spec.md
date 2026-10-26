# Tradio v3 — completed 15-minute candle signals

This supersedes v2's intraday strategy. Daily signals from v1 continue separately.

## Signal and timing

During a session, SMA8/SMA13/SMA21 use the last 8/13/21 **completed 15-minute candle closes**, across trading sessions. The bullish state is SMA8 above both other averages; bearish is below both; ties and intermediate positions are mixed. SMA13 need not exceed SMA21, and price need not exceed the averages. A flip into bullish/bearish fires only if its direction differs from the previous signal. Merely remaining in the same state produces no alert.

Check five seconds after each candle closes. Normal NSE session: 09:30:05, 09:45:05, …, **15:30:05 IST**, including the final 15:15–15:30 candle. Upstox timestamps denote candle **opening** times. The 14:00 candle is evaluated after 14:15:05. Ignore candles whose closing time plus the publication delay has not passed. Session hours come from Upstox, including holidays and special sessions; scheduling aligns to the session's open. One additional check 15 minutes after the close can recover a late final candle. A later tick processes any newly available completed candles in chronological order.

These are completed-candle signals. There are no provisional/undone/evening-confirmation messages for this timeframe, and no three-cross daily cap. A later opposite cross is a new signal. Separate **daily** crossover alerts still arrive after the official NSE close; they never cancel a 15-minute signal. All alerts label the timeframe and chart links select `15` or `D`. `/list`, `/add` status and `backtest` still refer to daily data and are labelled accordingly.

## Data and recovery

Use our existing Upstox Analytics Token and provider:

- Current session: `GET /v3/historical-candle/intraday/NSE_EQ|<ISIN>/minutes/15`.
- Previous sessions: `GET /v3/historical-candle/NSE_EQ|<ISIN>/minutes/15/<to>/<from>`.
- Fetch 28 calendar days of history on first evaluation and once per new day, within the API's one-month request limit. Fetch today's candles on each check. Sort and deduplicate timestamps. Store only candles matching actual session slots that have fully closed.
- `candle_prices(symbol, start)` persists raw closes. `candle_progress(symbol)` persists the processed timestamp, last signal direction and historical refresh date in Postgres. Retain 28 calendar days of cached closes.
- Compute a baseline from history. For a newly watched stock/first v3 run, only the most recent completed candle can alert; older history seeds indicators and direction without sending old buys. If that candle isn't available, seed silently and recover subsequent candles later.
- Existing progress replays missed candles in order. Signal and delivery creation, progress and cache changes commit together. A transaction advisory lock serialises candle checks from multiple invocations. Unique alerts use `(symbol, date, kind=candle, seq=opening minute of day)`; each alert also stores its exact candle timestamp.
- Missing expected candles restart the indicator window. Wait for 21 continuous completed candles to establish a baseline, then the next candle can trigger. Overnight closures/holidays are not missing bars. History outside the 28-day recovery window cannot be replayed; the available history reseeds indicators. Recovered events over five calendar days old update progress silently; existing delivery age limits also apply.
- **Corporate actions:** raw candle prices must not be averaged across a split/bonus/demerger. Conservatively restart the candle window on the latest known NSE ex-date. Before evening files arrive, detect an adjustment by comparing Upstox's adjusted previous close against the most recent official NSE daily close (0.2% tolerance). This intentionally requires warm-up after an action rather than manufacturing a cross from an unadjusted price gap. Daily history must be current; catch up the evening ingest first, or pause candle checks and notify the admin. As in v2, previous-close mismatches are a heuristic and can also suppress signals when sources disagree.
- No Upstox token/API access: skip intraday; daily ingest/alerts continue. Failures notify the admin once a day. No separate data subscription or websocket is required for this polling design.

## Commands and deployment

`tradio intraday --dry-run` prints the latest completed candle's timestamp, close, SMAs and state. It makes no writes and sends nothing. `tradio intraday` performs one session-aware check and delivers new alerts. `/testalert` previews candle and daily formats using existing daily sample values; it is a formatting preview, not a historical 15-minute replay.

Production runs in Kubernetes namespace `tradio-app`, deployment `bot`, with one replica and `Recreate`. The scheduler runs inside that pod. Never run a second Telegram poller locally. The ops manifests live in `~/projects/ops/tradio-app-k8s/`.

Rollout:

1. Run tests against a disposable local Postgres database, including migration upgrade/downgrade and schema checks. Never point pytest at production: the suite resets its database schema.
2. Build/publish an immutable app image through CI. Pin the same image in the migration Job (`04-migrate.yml`) and bot deployment (`05-bot.yml`).
3. Run the migration Job (`tradio db upgrade`) and wait for success **before** updating the bot. The migration adds candle tables and a nullable alert timestamp; existing daily/provisional data remains intact. The evening run can still reconcile old v2 provisional alerts.
4. Roll the single bot replica with `Recreate`. Verify logs, `/status`, token access and `kubectl -n tradio-app exec deploy/bot -- tradio intraday --dry-run` during/after a session. The first run seeds history automatically.
5. To roll back, restore the previous image; leave the additive schema in place. Do not downgrade a live database containing candle alerts: older enum readers cannot decode those alerts. Clear/resolve pending `candle` deliveries before running an older sender, or ship a compatible rollback image.

## RIL validation

A read-only Upstox replay on 26 October 2026 reproduced the supplied RELIANCE chart's **23 October 14:00** candle: close 1186.80, SMA8 1177.50, SMA13 1173.923076…, SMA21 1177.209523…. The preceding candle was not bullish; this candle flips bullish. Expected evaluation: **14:15:05 IST**. A fixed 22-candle fixture covers this exact case without network access.
