# Tradio v2 — Spec (changes from v1)

> Historical specification. See [v3](v3-spec.md) for the current completed 15-minute candle strategy and cluster deployment.

v2 sends the same daily SMA 8/13/21 cross during market hours instead of after the close. Everything in [v1-spec.md](v1-spec.md) still applies unless changed here.

## 1. Decisions

| Area | Decision |
|---|---|
| Signal | Unchanged: daily SMA 8/13/21 state flip with alternating directions (v1 §3). |
| Intraday | Every 15 min during the NSE session, today's live price stands in for today's close. A cross on that provisional close is alerted straight away. |
| Window | From 15 min after the open to 15 min before the close (9:30–15:15 on a normal day). The first 15 minutes are skipped as too noisy. Session hours come from Upstox, so holidays and special sessions (Muhurat, Budget Sunday) are handled. |
| Cap | At most 3 cross alerts per stock per day. The third says no more intraday alerts for that stock today. |
| Undo | If a provisionally alerted cross no longer holds on a later tick, the user is told ("undone"), unless the cap was reached. |
| Close | The evening run (v1 §6) confirms with NSE's official close: **confirmed** if the cross held, **not confirmed** if an alerted cross didn't hold, or a normal alert if the cross happened only at the close. |
| Replaces v1 | v1's evening alert becomes the confirmation step. Users still get every confirmed cross. |
| Data | Upstox Analytics Token: read-only, valid 1 year, free, GET only. Personal/order endpoints return 401 without a registered static IP, and none is registered. |
| Degraded | No token, Upstox down or the token expired: intraday is skipped and the admin is told once a day. The evening run carries on as v1. |

## 2. Upstox (verified 2026-10-18)

- `GET /v3/market-quote/ltp?instrument_key=NSE_EQ|<ISIN>,…`: up to 500 keys per call. Returns `last_price` and `cp` (previous close).
  - `cp` equals NSE's official previous close for every stock checked (2,679), and is already adjusted on an ex-date (BANSALWIRE split 5→1 on 16 Oct: `cp` 63.10 = 315.60 / 5).
  - `last_price` is the last trade, not NSE's official close (a weighted average for most stocks). For the 200 most-traded stocks they were identical on 16 Oct; across all stocks the median difference is 0, the 90th percentile 0.8%. The evening confirmation absorbs this.
- `GET /v2/market/timings/{date}`: NSE session start/end for a date, empty on holidays.
- Rate limit 50/s, 500/min. One tick needs 1 call per 500 watched stocks.
- The token is a JWT; its `exp` claim gives the expiry. The admin is reminded 30, 7 and 1 days before.

## 3. Provisional signal

For each watched stock, on a tick on trading day `T`:

1. **History must be current:** the latest day in the database must be the previous NSE session (from Upstox timings). If not (laptop was asleep through the evening run), the evening ingest runs first.
2. **Corporate action today:** if `cp` differs from our last stored close by more than 0.2%, `cp / last close` is today's adjustment factor (split, bonus, demerger) and all history is scaled by it for this calculation. NSE's evening files store the real action as in v1.
3. Append `(T, last_price)` to the adjusted closes and run the v1 signal (`analyse`). An event dated `T` is a provisional cross.

Alternation (v1 §3) means only one direction is possible on a given day, so a day's state per stock is: no cross / crossed / undone.

## 4. Per-day state and alerts

```
intraday_signals  (symbol, trade_date) PK, direction, active, crosses, price, sma8, sma13, sma21, updated_at
alerts            id PK, symbol, trade_date, kind [cross|undone|confirmed|not_confirmed], seq,
                  direction, price, sma8, sma13, sma21, after_intraday, created_at
                  UNIQUE (symbol, trade_date, kind, seq)
deliveries        (alert_id, user_id) PK, status, attempts, sent_at, error      -- was event_id
admin_notices     key PK, created_at                                            -- send-once admin messages
```

Each tick, per stock:

| Was | Now | Action |
|---|---|---|
| not active | cross | `crosses += 1`, active. Alert **cross** if `crosses ≤ 3` |
| active | no cross | not active. Alert **undone** if `crosses < 3` |

Evening, for each newly loaded day:

| Intraday | Official close | Alert |
|---|---|---|
| crossed (any) | event, same direction | **confirmed** (after_intraday) |
| none | event | **confirmed** (crossed at the close; reads like a v1 alert) |
| active, or capped | no event | **not confirmed** |
| undone and not capped | no event | nothing (the user already saw "undone") |

Deliveries, digests, retries, blocked users and the 5-day catch-up limit work as in v1, keyed by alert instead of event.

## 5. Runtime

- One 15-minute job in the bot runs the intraday tick (inside the session window) and the evening tick (18:00–22:00), plus the evening catch-up at startup.
- `tradio intraday [--dry-run]`: one intraday tick now. `--dry-run` prints each watched stock's provisional state and sends nothing.
- Config adds `UPSTOX_ANALYTICS_TOKEN`.

## 6. Out of scope for v2

Websocket streaming (15-minute polling is enough) · intraday candles/indicators · showing the provisional state in `/list`.
