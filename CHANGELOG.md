# Changelog — weatherbotPreYes0910

## 2026-09-12 — 统一 YES 吃单带为 0.75（fire 范围 ≡ live taker 带门）
- `config/yes2re_reversal.json`：`consensus_lock.yes_max_ask` **0.80 → 0.75**；`strategy_consensus_lock.py` 内置换默认同步 0.75。
- 原因（操作者拍板）：live 端口 taker 带门读 `strategy.yes_max_ask`（= 0.75），而 PreYes 入场顶价原为 0.80 ⇒ fire 限价落在 **(0.75, 0.80]** 时实盘会 `yes_price_above_band` 跳过（"fire 却不成交"），纸面/实盘同区间分歧。统一后三处一致：`consensus_lock.yes_max_ask` = `strategy.yes_max_ask` = `risk_control_yes_cap` = **0.75**。
- `tests_port.py`：配置防漂移 GOLDEN 快照同步更新。
- 验证：`tests_port` 31/31、`tests_live` 51/51、`tests_consensus_lock` 11/11、`tests_cycle_consensus_lock` PASS、`tests_fill_gate` 6 场景、`tests_reversal` PASS、sleeve 13/13 + 4/4。

## 2026-09-12 — 激进吃单 (FAK Taker) 精度修复 + Nautilus Trader v2.0 规范对齐 + 全量 142 单测通过

- **激进吃单 (FAK Taker) 生产精度对齐 (`live/v2_transport.py`)**：
  - 同步 Nautilus Trader v2.0 Polymarket 官方适配器规范与 Polymarket CLOB v2 最新限额：
    - `OrderType.FAK` 市场买单 (`BUY`) 的 `maker_amount`（USDC 名义金额）严格保留 2 位小数（`Decimal('0.01')`，向下取整）。
    - 市场卖单 (`SELL`) 的数量严格保留 4 位小数（`Decimal('0.0001')`，向下取整）。
    - 彻底修复 `400 invalid amounts, the market buy orders maker amount supports a max accuracy of 2 decimals` 报错。
  - **FAK 无对手盘静默撤单**：捕获 `400 "no orders found to match with FAK"` 响应并作为 0-fill 正常结算，消除残留风险虚假报警。
  - **腿级独立决策与严格放弃 (Take-or-Nothing)**：YES 腿在价格区间 `(0.45, 0.75]` 内执行 FAK 吃单，区间外严格放弃（`status="skip"`），**绝对不降级为被动挂单 (never passive fallback)**；NO 腿独立依据自身盘口 ask <= cap 决定是否吃单。
- **PreYes 稳了参数基线与回归测试套件全面对齐 (`tests_port.py`, `tests_live.py`)**：
  - 更新 `tests_port.py` 的基准黄金配置与策略期望，使其与 PreYes 实际配置（`fire_budget_usdc=15.0`, `paper_initial_capital_usdc=700.0`, `yes_min_ask=0.45`, `yes_max_ask=0.75`）完全一致。
  - 优化 `tests_live.py` 中的限价与可成交性测试，防止由于 PreYes 的 0.75 上限导致 0.80 价格覆盖误触发价格超限。
  - **测试全绿**：`tests_port.py` (31/31 PASS)、`tests_live.py` (51/51 PASS)、`tests_consensus_lock.py` (11/11 PASS)、`tests_cycle_consensus_lock.py` (2/2 PASS)、其余核心回测套件 (47/47 PASS)。总计 142 个测试用例在 Windows 与 WSL Linux 双环境下保持 100% 通过。

## 2026-09-11 — LIVE 执行层 Phase 1-3b 完整迁移 + CLOB v2 适配 + PreYes 稳了策略兼容

- **执行端口化架构 (`live/port.py`, `_r_cycle.py`)**：
  - 遵循 **live ≡ paper** 核心原则（同策略、同判定、同基建、同账本），执行差异封装于成交通道：`PaperPort` 走内存 FAK 撮合，`LivePort` 走 CLOB v2 真实下单与对账。
  - 在 `_r_cycle._paper_fire` 接入 `get_port(cfg)`。在 `mode=live` 且未满足三重闸门时抛出 `PortRefused` 并记录 `fire_port_refused` 审计事件，**绝对不开仓、绝不静默降级为 paper**。
- **PreYes 稳了策略专用腿兼容 (`live/order_plan.py`)**：
  - 适配 `strategy_consensus_lock.py` 独有的 `buy_yes_lock` 腿，将其纳入 `BUY_DIRECTIONS`，并映射上限至 `cfg['yes_max_ask']`（0.75），完美兼容稳了策略的高胜率锁定信号。
- **CLOB v2 全面迁移 (`live/v2_transport.py`, `live/clob_client.py`)**：
  - 淘汰已失效的 CLOB v1，全面采用 `py-clob-client-v2`。
  - 凭据显式传入，撤单统一调用 `cancel_orders([id])`，查单使用 `get_open_orders()`。
  - 下单前必须重新获取盘口并夹紧限价（`clamp_limit` + `refetch_book`），避免 `order crosses book`。
  - 成交对账轮询获取真实成交量和均价。撤单失败重试 3 次，仍失败标记 `residual_risk=True`。
  - 25 个写操作方法默认全部装载运行时抛异常哨兵，仅最小权限开放 `post_order` 与 `cancel_orders`。
- **生产级三重安全闸门 (`live/risk_gate.py`, `live/submit.py`)**：
  - 1) CLI / 环境变量 `YES2RE_LIVE_ENABLE_SUBMIT=1`
  - 2) 机器级标志 `LIVE_SUBMIT_ENABLED=1`（不写进 `.env`，防止误起）
  - 3) 当日 UTC 动态短语 `YES2RE_LIVE_CONFIRM=SMOKE-<YYYY-MM-DD>`（跨日自动失效，防止无人值守放量）
- **单配置双实例环境覆盖 (`_r_state.py`)**：
  - `config/yes2re_reversal.json` 保持 `mode: "paper"` 防止误触。
  - 支持 4 个环境变量覆盖：`YES2RE_MODE`、`YES2RE_FIRE_BUDGET_USDC`、`YES2RE_MAX_OPEN_POSITIONS`、`YES2RE_INITIAL_CAPITAL_USDC`。非法值严格 fail-closed。
- **网络与海外 VPS 直连兼容 (`market_ws_transport.py`)**：
  - 移植 `resolve_default_proxy()` 及直连 TLS，自动识别海外无代理直连环境与本地开发代理环境。
- **测试套件与运维工具**：
  - 新增 `tests_port.py` (16/16 PASS) 与 `tests_live.py` (50/50 PASS)。原有 6 个策略与流程测试套件 100% 保持通过。
  - 引入 `DEPLOY_RUNBOOK.md`、`ops/PENDING.md`、`ops/repair_log_live.md`、`run_live.sh`、`scripts/analyze_events.py`、`scripts/monitor_live.py`。


- **`reversal_strategy.py` fire window switched from single-edge bounds to
  inclusive local hour intervals.** One-bucket reversal fires now gate on:
  - HIGH: local `13 <= hour <= 17` (was `hour >= 14`, no upper bound)
  - LOW: local `1 <= hour <= 9` (was `hour <= 10`, no lower bound)
- Constants: `HIGH_FIRE_LOCAL_HOUR` / `LOW_FIRE_LOCAL_HOUR_END` removed →
  `HIGH_FIRE_LOCAL_START=13` / `HIGH_FIRE_LOCAL_END=17` /
  `LOW_FIRE_LOCAL_START=1` / `LOW_FIRE_LOCAL_END=9`. `hour_ok` now takes the
  four window bounds and enforces `start <= h <= end` per direction. Both `arm`
  and the pre-fire `hour_not_in_window` gate use the same window. `prune`
  low-zombie sweep follows the new low end (9).
- Config keys: `high_fire_local_hour` / `low_fire_local_hour_end` →
  `high_fire_local_start` / `high_fire_local_end` /
  `low_fire_local_start` / `low_fire_local_end` (`config/yes2re_reversal.json`
  updated; old keys removed).
- **Rationale:** the daily extreme (and the capped peak-tick reversal this
  strategy sells) forms inside the window, not outside it. Real losses from
  out-of-window fires: mexico-city low 02:02 (LOST), SF 9/7 01:00 local fire
  (open, floating underwater) — both broke the reference at hours the peak
  window does not span. Fires observed off-window are now suppressed.
  Direction-specific windows also stop one city's LOW-break drift from firing
  into the afternoon or a HIGH from firing predawn. On-window losses of the
  chengdu class are a separate open question (bucket-break confirmation) and
  are not addressed by this change.
- Verified: `python3 tests_reversal.py` 16/16 PASS before and after (all 16
  scenarios keep passing under the interval semantics); window-boundary
  assertion (high 12/18 rejected, 13..17 accepted; low 0/10 rejected, 1..9
  accepted) green.

## 2026-09-07 — F-market unit audit + boundary-confirmation margin

- **Polymarket unit rules audited & documented** (`research/common.py`
  `c_to_market_unit` docstring):
  - Buckets: US cities 1-2°F integer buckets; EU/Asia cities 1°C buckets.
  - Resolution: Wunderground station "Daily Observations" — finalized daily
    extreme at whole degrees, post-QC (NOT intraday METAR, NOT the NWS CLI
    summary, NOT the WU "Day High & Low" box). Stated precision rule is
    truncation for °C buckets (23.9°C → 23).
  - METAR has NO native °F anywhere (global °C, incl. US ASOS). US ASOS
    displays whole °F via rounding — our °C→°F round matches that display
    convention; the Polymarket truncation rule applies to the °C-bucket side
    where whole-degree METAR already aligns naturally.
- **F-market break-confirmation margin** (`reversal_strategy.py`,
  `break_confirm_margin_f` default 1.0, config key added): a °F-market fire
  requires the whole-degree converted extreme to clear the broken-bucket
  boundary by ≥1°F. Motivation: SF 9/4 low misfire — METAR 14°C converted to
  57.92°F < 58 (break), but Wunderground finalized 58.x°F (no break): METAR
  whole-°C granularity spans ±0.9°F after conversion and the finalized daily
  extreme can differ ~1°F from the intraday METAR extreme.
- **Back-test on real °F fills (2026-09-05→07, 5 trades)**: margin=1 would
  have kept SF 9/6 (YES@0.52 WON) and SF 9/7 (open), and filtered chicago
  9/5 low (NO@0.97 LOST — the SF-class false break) — but it would also have
  filtered atlanta 9/6 low (NO@0.92 WON + YES WON) and austin 9/5 high
  (YES@0.98 WON), both genuine near-boundary breaks. Trade-off is documented
  and tunable: 0.0 = legacy float behavior (fire all near-boundary breaks),
  1.0 = filter all <1°F-deep breaks (default, prevents SF-class false
  breaks at the cost of genuine near-boundary fills). C markets are exempt
  (whole-degree truncation aligns exactly).

## 2026-09-04 — Fire deadlock fix; WS live feed; paper-ledger fix (audited)

- **obs sanity window (was: absolute 180 s age gate → structurally zero fires).**
  METAR/SPECI obs_time age swings 0-60 min on hourly cadence (US AWS publish
  ~7 min early); `require_fresh_obs_seconds=180` made `stale_obs` block every
  fire. Replaced with sanity window `max_obs_lookback_seconds=5400` /
  `max_obs_future_seconds=900`: any NEW observation (deduped by
  `is_new_obs_time`) may fire unless the feed is >90 min behind or the stamp
  is >15 min in the future. First live fire within 27 min of deploy.
- **Full skip audit.** `_r_cycle` no longer silently drops skips: every
  re_skip / re_skip_yes / re_disarm is logged with reason/jump/consensus
  (silent skips previously hid the 0-fire deadlock).
- **NO cap 0.65 → 0.85** (broken-bucket NO redeems ~1.0; wider cap = fills);
  YES leg cap unchanged 0.48.
- **Universe: 10 → all 49 cities** (drop `active_icaos` allowlist; both high
  & low directions). `idle_metar_interval_seconds` 45 → 60 (49 cities = 3
  CheckWX batches; 4320 req/day < 5000 paid cap).
- **Market WebSocket live** (`market_ws_transport.py` stdlib-only WS client
  through the CONNECT proxy + `ws_bridge.py` daemon thread). 2000+ tokens
  subscribed; fresh (<5 s) WS LocalOrderBook snapshots overlay the ladder
  cache (epoch-guarded, never clobbers newer REST data); auto-reconnect
  5/10/30 s; REST /books remains the correctness backbone (the public market
  channel is near-frozen per py-clob-client #292 — WS is an accelerator).
- **Paper ledger fix.** `release()` no longer clamps total debit to zero —
  a negative debit is realized profit (equity = initial − debit). The clamp
  had silently discarded +52.80 USDC of paper profit (cost 49.06 vs payout
  101.86). `total_debit_usdc()` now reads negative values directly instead of
  through the `parsed >= 0` filter.
- **Audit hardening (pi + omp cross-review 2026-09-04):** `ensure_tokens`
  and `mark_disconnected` thread-safety (dict-size-change race during
  reconnect); `_ws_pump` epoch comparison made real (docstring now honest).
- Verified: 7/7 scenario tests; equity 1000 → 1052.79 after first US market
  settlements (NO legs 4/4 wins; one YES lottery leg lost).

## 2026-09-03 — Dual-rate paper runner; no σ; real-API soak

- **Zero σ / bias / fade-NO / dead-NO / BUY-YES** on the run path.
- Dual-rate METAR/books (ARM ~8s; idle METAR ~45s + consensus books ~30s).
- Dual-source METAR (CheckWX + AWC); C/F via `c_to_market_unit`; Gamma rules cache 20min.
- Modules: `runner_impl.py`, `_r_globals.py`, `_r_state.py`, `_r_data.py`, `_r_cycle.py`, `_r_exec.py`.
- **10-min paper soak (real CheckWX+Gamma+CLOB):** 49/49 METAR, 98 rules, Atlanta+Denver ARMed, 0 FIRE, capital 1000 USDC, no cycle_error.

## 2026-09-02 — Merge poly-yes2 paper infra; drop σ

- Not merged: TAF/σ arms. This repo is strategy + paper runtime.
