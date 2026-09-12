# Changelog — weatherbotPreYes0910

## 2026-09-12 — 新增并行通道 `buy_yes_next`「下一档桶廉价入场」（**代码默认关闭，config 显式开启**）

**决策依据（实测，非推断）**：`/home/da/桌面/poly-yes2/preyes_param_sim_20260912.md` §4/§5/§6。
既有目标桶通道的非价格门**全部通过**时，目标桶 ask 已被市场定价到 **0.81–0.99**，被最后一门
`yes_max_ask=0.75` 挡死 ⇒ 8 小时 **零成交**（`entry_count=0`）；时间对齐证明 TWAP/instant 放宽
的**边际新增机会 = 0**（几十秒自解），唯一能多出机会的参数是 cap，但那条路是"追高"、两种口径
EV 皆负（−10%~−25%）。结论：要「极高胜率 + 廉价入场」必须**改入场对象** ⇒ 在"下一档桶"报价
低廉时直接建仓该桶。

- **新通道 `buy_yes_next`**（并行、优先评估）：触发前置 = 与既有通道**逐字一致的非价格门**
  （站点频次 → 时间窗 → 速度/变率 → 预期极值桶位已确认 → 共识 **rank1**），价格约束换成该通道
  **自有且独立**的窗口 `(next_entry_min_ask, next_entry_max_ask]`（默认 **0.20 不含 / 0.32 含**）。
  **区间外 ⇒ 彻底弃单**：不降级、不挂被动单、不回退既有窗口。
  > 语义替代（有意为之）：下一档桶的 **twap/instant 价格子门**（0.26/0.25/0.15）是既有目标桶
  > 通道的过滤器，新通道用自有窗口替代它 —— 目标桶通道的窗口/门序/判定价逻辑**一字未改**。
- **与既有通道的关系**：新通道命中 ⇒ 既有通道该 tick 不下单；新通道弃单 ⇒ 既有目标桶通道按原
  逻辑（窗口仍为 (0.45, 0.75]）独立判定。**预算隔离**：新通道用
  `next_entry_budget_pct`（默认 0.5）× fire 预算；既有通道只用剩余额度（`15 − 7.5 = 7.5`）。
  会话上限沿用既有 `max_fires_per_session` 语义（不新增超出既有上限的并发）。
- **腿级窗口贯通到 live 端口 taker 带门**：新通道腿自带 `floor`/`cap`，`LivePort.fill_mode`
  以**腿自带窗口**判定（`entry_channel == "next_bucket"`），cfg 的 `yes_min_ask/yes_max_ask`
  动不了它；腿窗口缺失/非法 ⇒ `leg_window_unusable` **fail-closed**（绝不回退成 cfg 窗口）；
  仅"完全无腿窗口"才按规范回退 cfg。ladder intent 现在透传 `floor`/`entry_channel`（加法式）。
- **审计**：`intent`/`submit` 与 `data/live_events.jsonl` 新增 `entry_channel`
  (`next_bucket`/`target_bucket`)、`leg_window`、`window_source`、`next_entry_window`；
  通道字段由**专用入参**盖章（合并 `audit_extra` 之后），外部 `audit_extra` 同名值先被剔除
  ⇒ **不可伪造/翻转**。`data/yes2re_events.jsonl` 的 `fire` 行同样带通道字段。
- **配置**（`consensus_lock` 块）：`next_entry_enabled` / `next_entry_min_ask` / `next_entry_max_ask` /
  `next_entry_budget_pct`。`strategy_consensus_lock.py::DEFAULT_CONFIG` 同名键但
  **`next_entry_enabled: false`**（代码默认保守）；本仓 `config/yes2re_reversal.json` 显式开启。
- **引擎入口 fire 构造提取为 `_r_cycle.consensus_entry_fire(...)`**（可测的生产分支）：两通道的
  腿/窗口/预算/审计字段在此一处组装；既有目标桶通道的 fire 内容逐字不变（仅新增 `entry_channel` /
  `next_entry_window` / 预算量化）。顺带修一处**绑定缺陷**：HEAD 的入口 fire 分支引用 `ZoneInfo`，
  而该名字只由 TAF 块内的一处 local import 绑定（同一函数作用域）⇒ **有 TAF 时正常、无 TAF
  （`market_rank1` 回退）时 `UnboundLocalError` 并中断整轮**（同类缺陷在本仓已有先例：sleeve 调用点
  的热修注释）。提取后函数自行绑定 `ZoneInfo` ⇒ 常规行为不变、无 TAF 时不再炸。**未动**：破位反手
  fire 分支（`_r_cycle` 内同一 `ZoneInfo` 形状）保持原样，属既有缺陷，需操作者另案决定。
- **验证**（原始输出见 `/tmp/preyes_next_bucket_impl_report.md`）：`tests_consensus_lock` 15/15、
  `tests_cycle_consensus_lock` 4/4、`tests_port` **33/33**、`tests_live` 51/51、`tests_reversal` 24、
  `tests_fill_gate` 6、`tests_sleeve_signal` 13、`tests_sleeve_wiring` 4、`tests_market_adapter` 4；
  边界矩阵 0.199/0.200/0.201/0.319/0.320/0.321 ⇒ 弃/弃/入/入/入/弃（策略层 + 端口层各一套）；
  带外 `execute_leg` 调用数 = 0；既有通道 33 场景投影与 `ccbd0c8` **逐场景一致**（默认关闭 0 差异；
  开启时 5 处差异全部是"新通道成交"）；`paper_reversal_sim.py --scenarios-only` sha256
  `3d16632c…` **与基线逐字一致**；3 组变异（区间外降级 / 闭区间 / 忽略预算隔离）全部被对应用例捕获
  并逐字还原（sha256 复原）。
- **风险（如实记录）**：EV 前提「市场系统性低估该桶」**未经结算验证**（8h 样本无法证实，见报告
  §5.3）⇒ 通道默认关闭、部署保持小额（本仓 = 0.5 × 15 USDC = 7.5 USDC 名义/次，且受 live 端
  `LIVE_FIRE_BUDGET_USDC`/`LIVE_MAX_CAPITAL_USDC` 约束）；单笔最大损失 = 该通道预算；一键关闭 =
  `next_entry_enabled: false`（立即回到今日行为，既有通道逐行不变）。

## 2026-09-12 — 修 settle_failed 根因（裸 socket 读超时中断整轮结算；同步自共享引擎）

- **同缺陷**：`market_adapter._fetch_json` 未处理裸 `TimeoutError`（socket 读超时不包成 `URLError`）→ 穿透 `fetch_market_resolution` 的窄捕获 → `_r_cycle` 记 `settle_failed` 并**中止整轮结算**。两仓 `market_adapter.py` md5 相同，属共享引擎缺陷。
- **修复（操作者选 A）**：`_fetch_json` 增 `except TimeoutError → RuntimeError` ⇒ 超时按 unresolved 返回 `None`，由 `settle_poll_seconds` 下轮重试，不再中断整轮。
- **验证**：新增 `tests_market_adapter.py` 4/4；本仓全绿（`tests_port` 31/31、`tests_live` 51/51、`tests_reversal`、`tests_fill_gate`、`tests_consensus_lock` 11、`tests_cycle_consensus_lock`、`tests_sleeve_*` 13/4）。

## 2026-09-12 — CRITICAL: 修 neg_risk 签名域丢失（实盘 fire 100% 下不出去）+ 引擎预算与 live 上限对齐

**背景（真实链路演练实测暴露，非推断）**：用引擎自身链路 `_r_cycle._paper_fire` 在真实市场下单，连续 3 次被 CLOB 拒绝：
```
400 {"error":"invalid POLY_PROXY signature"}
```
链路本身正常（`plan_fire_cycle` → `send_fak limit=0.72 shares=10` → 端口 taker 决策正确），
失败发生在签名域：**天气桶市场是 neg-risk 市场**，而引擎的梯子缓存里没有 `neg_risk` 字段。

- **根因**：`_r_cycle._normalize_snapshot()` 只透传 `best_ask/best_bid/tick_size/asks/bids`，
  丢掉了 `execution/market.py` `BookView` 里已有的 `neg_risk`（和 `min_order_size`）。
  `live/port.py` 于是 `bool(book.get("neg_risk"))` = False ⇒ 按**非** neg-risk 交易所签名
  ⇒ CLOB 判签名无效。**只要市场是 neg-risk，实盘 fire 就永远发不出去**（静默失败）。
- **修复 1（根因）** `_r_cycle._normalize_snapshot`：透传 `neg_risk` / `min_order_size`。
  这两项纯元数据，`paper_match_fak` 不读 ⇒ paper 行为不变（`paper_reversal_sim --scenarios-only`
  输出 sha256 `3d16632c…` 与基线逐字一致，已实测）。
- **修复 2（纵深防御）** `live/port.py LivePort.resolve_neg_risk()`：盘口缺该字段时用 live 客户端
  重取一次盘口（CLOB `/book` 带 `neg_risk`，与 `refetch_book` 同源）并按 token 缓存；
  **仍取不到 ⇒ 拒单（`neg_risk_unknown`，fail-closed）**，绝不按错误签名域硬发。
  审计行新增 `neg_risk` / `neg_risk_source`。
- **修复 3（配置对齐，服务器 .env）**：引擎 fire 预算来自 `cfg.fire_budget_usdc`（config=**15**），
  而 live 端口用 `check_limits(notional=15, fire_budget=LIVE_FIRE_BUDGET_USDC=10)` ⇒
  `15 > 10` ⇒ **每次 fire 被 `limit_fire_budget` 拒绝**。故在服务器 `.env` 增加引擎级覆盖
  `YES2RE_FIRE_BUDGET_USDC=10`（尊重操作者设定的单笔实盘上限 10，且不改共享 config）。
- **验证（真实资金，磨损可接受）**：修复后重跑引擎链路演练 ⇒ **真实成交**
  `Shanghai 28°C`：`buy_yes_new send_fak limit=0.66 shares=10.00 → filled 10.153845 @0.66`
  （账本记账 cost 6.701538 USDC），随后 FAK 卖回 `10.15 @0.56`（`0xc84665f7…`），
  账户回零（balance 51.375982→50.219453，positions 0，open_orders 0），净磨损 1.156529 USDC。
- 测试：`tests_port` 31/31、`tests_live` 51/51、`tests_consensus_lock` 11/11、
  `tests_cycle_consensus_lock` 集成全绿、`tests_fill_gate` 6 场景、`tests_reversal`、
  `tests_sleeve_signal` 13/13、`tests_sleeve_wiring` 4/4。
- 说明：演练写入生产 `data/live_events.jsonl` 的 4 行（2 笔订单，00:41:31 BUY / 00:41:39 SELL）
  是**演练单**，非策略 fire。

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
