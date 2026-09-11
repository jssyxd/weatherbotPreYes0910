# weatherbotPreYes0910 / weatherbotyes2re

METAR vs consensus **reversal** + **PreYes "稳了" Consensus Lock-in Strategy**.

- **PreYes Strategy (`strategy_consensus_lock.py`)**:
  - Filter: 22 high-cadence METAR stations (<=30 min report interval).
  - Time Window: HIGH 12:00-18:00 local, LOW 00:00-09:00 local.
  - Signal: METAR reaches expected extreme + rank-1 consensus + next bucket 1h TWAP < 20¢.
  - Execution: Capped Taker with safety ceiling (0.45, 0.75].
  - Pre-METAR Stop-Loss: Early orderbook surge/bid-collapse fast liquidation.
  - Risk Lock: Max 2 fires/reverses per session to avoid multi-jump cascading loss.
  - Unit Tests: `python tests_consensus_lock.py` (8/8 PASS).

**No σ. No fade-NO / BUY-YES grid. Paper by default, with opt-in CLOB v2 Live execution.**

## Execution Ports (live ≡ paper)

- **Architecture**:
  - `PaperPort`: In-memory L2 FAK matching, zero network order execution.
  - `LivePort`: Polymarket CLOB v2 live execution with non-marketable limit orders (`postOnly`), TOCTOU book refetch, and fill reconciliation.
- Both ports share the identical strategy engine (`strategy_consensus_lock.py` + `reversal_strategy.py`), consensus logic, arming windows, leg sizing, and accounting rules.
- **Safety Lock**: `config/yes2re_reversal.json` defaults strictly to `mode: paper`. Live mode can only be activated via environment variables (`YES2RE_MODE=live`) guarded by three independent gates:
  1. `YES2RE_LIVE_ENABLE_SUBMIT=1`
  2. `LIVE_SUBMIT_ENABLED=1`
  3. `YES2RE_LIVE_CONFIRM=SMOKE-<UTC-date>`
  If any gate is missing, the engine raises `PortRefused` and fails closed (will **never** silently fall back to paper or execute unverified orders).
- Detailed live setup and operational guide: see [`live/README.md`](live/README.md) and [`DEPLOY_RUNBOOK.md`](DEPLOY_RUNBOOK.md).

## Rules

- High: `running_max`; Low: `running_min`.
- Reference: TAF TX/TN if present (converted to market unit), else 1–2h rank-1 YES TWAP mid.
- YES leg only if jump exactly 1 bucket; jump ≥ 2 → NO-only.
- New `obs_time`, age ≤ 180s; high local hour ≥ 14; low ≤ 10.
- Broken bucket must be rank-1 over `consensus_window_seconds` (default 7200).
- Legs: BUY NO broken (cap 0.65, 75%) + optional BUY YES new (cap 0.48, 25%), or PreYes `buy_yes_lock` (cap 0.75).
- Idle ~20s; **ARM** → fast poll those ICAOs (~10s) while **full universe** still samples books/METAR slowly for consensus.
- FIRE: in-memory L2 FAK (paper) or CLOB v2 non-marketable post-only limit (live). One fire per `city|date|direction`.

## Data path

| Need | Source |
|------|--------|
| METAR | **CheckWX + AviationWeather** (fresher wins) |
| TAF TX/TN | CheckWX (optional; market rank-1 fallback) |
| Units | METAR/TAF °C → city `market_unit` (°C/°F) |
| Buckets / tokens | Gamma REST (cached ~20 min) |
| Books | CLOB REST seed into `LocalOrderBook`; optional Market WS |

## Run

```bash
export CHECKWX_API_KEY=...   # still useful; AWC works without key
python3 tests_reversal.py
python3 tests_consensus_lock.py
python3 tests_port.py
python3 tests_live.py
python3 reversal_runner.py once --config config/yes2re_reversal.json
python3 reversal_runner.py run  --config config/yes2re_reversal.json
```

Logs: `data/yes2re_events.jsonl` · Health: `data/yes2re_health.json`

WSL: `networkingMode=mirrored` for Gamma/CLOB.

## Latency notes

- Prefer a low-latency VPS near Polymarket CLOB (often US East).
- Keep token ids + books warm before FIRE; do not discover tokens on the fire path.
- Bottleneck is METAR publish lag + CLOB RTT, not Python microbenchmarks.

## poly-yes2

This repo is the **canonical reversal paper & live** stack. Archive or ignore poly-yes2 treeB for reversal.
Keep poly-yes2 only if you still need the old three-arm / Hermes / settlement-review history.

## Safety

`reversal_runner.py` blocks unvalidated modes; config file cannot set `mode: live`. Live mode strictly requires environment variable overrides and triple-barrier gate confirmation.

