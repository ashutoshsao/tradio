# Tradio v1 — Spec

> Historical specification. See [v3](v3-spec.md) for the current completed 15-minute candle strategy and cluster deployment.

Telegram alerts when a watched NSE stock's daily SMA8 flips to above both SMA13 and SMA21 (bullish) or below both (bearish).

## 1. Decisions

| Area | Decision |
|---|---|
| Users | Owner + a few friends. Only allowlisted Telegram users can use the bot. |
| Watchlist | Per user, managed via Telegram: `/add`, `/remove`, `/list`. `/add` replies with the current SMA state. |
| Universe | Any NSE stock in the EQ series (originally Nifty 50; widened since every EQ stock is stored anyway). |
| Data | NSE bhavcopy (official daily file), free, no account. 2-year backfill once, then one file per trading day. |
| Signal | Daily SMA 8/13/21 on adjusted closes, **state flip** (see §3). |
| Timing | End of day only. No intraday checks. |
| Holidays | No holiday logic. No bhavcopy for a date = no trading day. |
| Splits/bonuses/demergers | Past prices adjusted so corporate actions don't create fake crosses (see §4). Rights issues not adjusted in v1. |
| Dedupe | One event per (stock, date, direction); one delivery per (event, user). Reruns are safe. |
| No-signal days | Nothing sent to users; run is logged. |
| Failures | Bhavcopy retried until 22:00 IST, then admin is notified. |
| Stack | Python, Postgres (Docker), python-telegram-bot (long polling). Laptop now, Kubernetes later. |

## 2. Data sources (verified 2026-10-17)

- **Bhavcopy (UDiFF format):**
  `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip`
  - Needs a browser-like `User-Agent`. Returns 404 for non-trading days / not yet published.
  - Columns used: `TradDt, TckrSymb, SctySrs, ISIN, OpnPric, HghPric, LwPric, ClsPric, PrvsClsgPric, TtlTradgVol`.
  - Keep rows where `SctySrs == "EQ"`.
  - NSE switched to this format in mid-2024; a 2-year backfill stays within it. (Older files use a different format; not supported in v1.)

- **Corporate actions, daily file:** `https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{DDMMYY}.zip`, member `bcDDMMYYYY.csv` (older: `BcDDMMYY.csv`).
- **Corporate actions, API:** `https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY` (needs a `Referer` header; bot-protected host, so failures are tolerated and retried next run).

All NSE access goes through a `MarketDataSource` interface so a broker API (e.g. Kite Connect) can replace it later.

## 3. Signal rule

For each stock and trading day `t`, using adjusted closes:

```
sma_n(t) = mean(close[t-n+1 .. t])

state(t) = ABOVE  if sma8 > sma13 and sma8 > sma21
           BELOW  if sma8 < sma13 and sma8 < sma21
           MIXED  otherwise (incl. ties)

event on t  if state(t) in {ABOVE, BELOW} and state(t) != state(t-1)
            and its direction differs from the previous event's
            ABOVE → BULLISH, BELOW → BEARISH
```

- Directions alternate: BELOW → MIXED → BELOW is SMA8 bouncing off one average, not a new cross, so it doesn't repeat the bearish alert. (Cuts the median from ~17 to ~13 alerts per stock per year.)
- No minimum margin: any cross counts, so alerts line up with chart crossovers.

- Fires whether SMA8 crossed both on the same day or one at a time across several days.
- A direct ABOVE → BELOW flip (gap move) also fires.
- Needs ≥ 22 closes (21 for SMA21 + 1 previous day). Guaranteed by the backfill.
- **Data gaps restart history:** if a stock is missing for more than 5 market trading days (suspension, moved to the BE series, a demerger's trade-for-trade period), its history restarts and it needs 21 continuous closes again before alerts resume. Averaging across the hole would mix prices from before and after an unknown change.
- Pure functions, no I/O, fully unit-tested. Verified against an independent source: SMA states recomputed from Yahoo Finance closes for 12 Nifty 50 stocks gave identical events, except where Yahoo has a phantom 15 Jan 2026 holiday bar, lacks the 1 Feb 2026 Budget-day session, or doesn't adjust for demergers.

## 4. Split / bonus / demerger adjustment

Raw prices are stored unchanged. Adjustments are stored separately and applied on read:

```
adjusted_close(d) = close(d) * product(factor for actions with ex_date > d)
```

**Why not use the bhavcopy's previous close?** The original plan assumed NSE adjusts `PrvsClsgPric` on ex-dates. Checked against real data, it doesn't, so prices alone can't tell a split from a crash.

**Sources.** Two NSE sources, each incomplete on its own (checked Oct 2024 to Oct 2026):

| Source | Misses |
|---|---|
| Daily `bc` file in the PR zip (reliable archives host) | Some splits/bonuses (e.g. SHRIRAMFIN 10 Jan 2025) and some demergers |
| Corporate actions API (bot-protected host) | ETFs |

Both are used. Each action gets a normalised key, so one reported by both sources is stored once:

| Purpose text (examples) | Key | Factor |
|---|---|---|
| `BONUS 4:1`, `Bonus 4:1` | `bonus 4:1` | held / (new + held) = 0.2 |
| `FV SPLT FRM RS 2 TO RE 1`, `Face Value Split … From Rs 2/- … To Re 1/-` | `split 2>1` | to / from = 0.5 |
| `Consolidation … From Re 1 … To Rs 10` | `consolidation 1>10` | 10 |
| `DEMERGER` | `demerger` | open on ex-date / previous close (no ratio is published; NSE's special pre-open session sets the price) |

Several actions on one ex-date multiply (Bajaj Finance, 16 Jun 2025: bonus 4:1 + split 2>1 = 0.1).

- **Ignored:** dividends, rights issues (v1 limitation; mostly small caps), bonus preference shares/debentures.
- **Only actions already ex are stored:** files and the API also list announced ones, which can still be rescheduled.
- **Check:** `tradio check-adjustments` compares each split/bonus factor with the actual overnight gap. On the 2-year backfill, 175 of 179 matched within 10%, and the 4 exceptions were normal daily moves on top of the split.
- **Demergers of smaller stocks:** the stock usually leaves the EQ series for ~2 weeks after the ex-date, so there's no ex-date price to derive a factor from. The data-gap rule (§3) covers these. Large stocks (ITC, HINDUNILVR, Tata Motors, Siemens, Vedanta) trade through and are adjusted.
- **Known gap: symbol renames** (e.g. TATAMOTORS → TMPV) start a new price history under the new symbol.

## 5. Data model (Postgres)

```
instruments      symbol PK, isin, name, series, last_seen_date
daily_prices     (symbol, trade_date) PK, open, high, low, close, prev_close, volume     -- raw, all EQ rows
trading_days     trade_date PK, actions_loaded bool
price_adjustments (symbol, ex_date, action) PK, factor, purpose, source, detected_at
users            telegram_user_id PK, chat_id, username, is_admin, is_allowed, created_at
watchlist        (user_id, symbol) PK, added_at
signal_states    (symbol, trade_date) PK, sma8, sma13, sma21, state
signal_events    id PK, symbol, trade_date, direction, close, sma8, sma13, sma21
                 UNIQUE (symbol, trade_date, direction)
deliveries       (event_id, user_id) PK, status [pending|sent|failed], attempts, sent_at, error
runs             trade_date PK, status, attempts, started_at, finished_at, error
```

Every EQ row is stored (~2,700/day, ~1.3M rows for 2 years), which is cheap and lets any stock be added instantly with full history. Signals are computed only for watched stocks.

## 6. Daily run

Scheduled every 15 min from 18:00 to 22:00 IST, every day, plus once when the bot starts (to catch up after the laptop was off). Each tick is idempotent:

1. Find dates after the last ingested date up to today. Fetch each bhavcopy and its `bc` file. 404 → skip (non-trading day or not yet published). Then query the actions API for the last 14 days.
   This also catches up automatically if the laptop was off for some days.
2. Upsert prices and actions → compute states for watched stocks → insert events (`ON CONFLICT DO NOTHING`).
3. Create a `deliveries` row for each (event, user watching that stock).
4. Send pending deliveries: **one digest message per user** with all their events. Mark sent/failed; failed ones retry on the next tick.
5. Catch-up events older than 5 trading days are marked skipped, not sent.
6. At 22:00 on a weekday with no bhavcopy for today: notify admin ("no bhavcopy for <date>: holiday or NSE delay").
7. If a tick throws, the admin is told once per day and the next tick retries. Users who blocked the bot have their deliveries marked skipped.

## 7. Telegram bot

User commands:

| Command | Behaviour |
|---|---|
| `/start` | Allowlisted → welcome. Otherwise → "Not authorised. Your ID is `<id>`", and the admin gets a message with that ID. |
| `/add SYMBOL` | Must be an EQ-series symbol in the latest bhavcopy; unknown symbols get a "did you mean" suggestion. Replies with current state, e.g. "RELIANCE added. SMA8 is above both (as of 16 Oct)". If the stock has fewer than 22 days of history (new listing), says how many trading days until alerts start. Alerts only for future flips. |
| `/remove SYMBOL` | Removes from watchlist. |
| `/list` | Watchlist with each stock's current state. |
| `/help` | Command list. |

Admin commands: `/allow <id>`, `/revoke <id>`, `/users`, `/status` (last run, last trade date ingested).
The admin is set via `ADMIN_TELEGRAM_ID`.

Alert digest format:

```
📈 Tradio — 16 Oct 2026

🟢 BULLISH  RELIANCE
SMA8 moved above SMA13 & SMA21
Close ₹1,211.20 · SMA8 1,205.4 · SMA13 1,198.7 · SMA21 1,201.2

🔴 BEARISH  TCS
SMA8 moved below SMA13 & SMA21
Close ₹2,465.00 · SMA8 2,401.1 · SMA13 2,430.6 · SMA21 2,452.3

Not investment advice.
```

## 8. Runtime

- One process, `tradio bot`: Telegram long polling + in-process scheduler for the daily run.
- CLI commands (also used by the scheduler):
  - `tradio backfill --years 2`: downloads past bhavcopies, about 1 request/sec.
  - `tradio run-daily [--date YYYY-MM-DD]`
  - `tradio backtest [SYMBOL]`: event counts per stock over history, plus the last N events, for checking against a chart.
- Config via env: `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, `ADMIN_TELEGRAM_ID`.
- Local: `docker compose` for Postgres; app runs natively. Missed days while the laptop is asleep are caught up on the next run (§6.1).
- Later on Kubernetes: a bot `Deployment` plus a `CronJob` running `tradio run-daily` (`*/15 18-21 * * *` IST). No logic changes, because each run is idempotent.

Libraries: python-telegram-bot, SQLAlchemy 2 + Alembic, psycopg 3, pandas, httpx, pydantic-settings, pytest.

## 9. Out of scope for v1

Intraday checks · indicators other than SMA 8/13/21 · non-EQ series (BE, SM, etc.; a stock moved to BE stops updating) · symbol renames · web UI · chart images · Kubernetes manifests · public bot.

## 10. Milestones

1. Skeleton: project layout, Postgres compose, migrations, config.
2. Data: bhavcopy client, 2-year backfill.
3. Adjustments: NSE corporate actions (bc file + API), verified against overnight gaps.
4. Signal engine: pure function, unit tests, backtest report. **Checkpoint: eyeball a few events on charts.**
5. Bot: allowlist, `/start /add /remove /list`, admin commands.
6. Daily run: scheduler, events → deliveries → digest, retries, admin failure alert.
7. Dry run: one week on the laptop, comparing alerts against charts.
