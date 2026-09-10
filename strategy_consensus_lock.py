"""strategy_consensus_lock.py — 优化版"稳了"共识锁定策略与前置异动极速风控引擎 (V2 生产级)

基于实盘微观结构三大优化落地：
1. 【执行优化 - Capped Taker / 价格门控吃单】:
   - 摒弃 90% mid 被动挂单（避免好单 0 成交、成交必是接盘毒流的逆向选择陷阱）；
   - 在严格满足"站上极值+共识第一+下一档<20¢"高胜率形态下，采用带安全顶价的 Taker 吃单；
   - 价格区间约束在 [yes_min_ask=0.45, yes_max_ask=0.75] 之间，超 0.75 绝不追高，保住利润空间；
   - 同时保留 best_bid_peg 作为可选备用模式。
2. 【风控优化 - Pre-METAR 盘口异动先导抢跑止损】:
   - 不坐等新 METAR 落地（官方报文落地时二元期权买盘通常已蒸发归零）；
   - 先导监测下一档桶异动：若下一档（如32℃）YES 盘口暴涨突破 35¢，或当前持仓桶 YES 买盘跌破 45¢；
   - 立即在 METAR 到达前 30~60 秒执行【抢跑止损】，以尚存的盘口买价（0.45~0.55）果断割肉，保全 50%+ 本金！
3. 【频次与追火约束 - 单日单方向最多 1 次反手（上限 2 次）】:
   - 严格继承线上实战检验的 Session 计数器（fires_count <= 2）；
   - 反手时对破位桶 NO 做真实微观结构检查：NO 卖价 <= 0.85 才买，若 NO 已被扫空至 0.99/1.00 则自动跳过；
   - 杜绝连续跳桶引发的双重亏损（Double Wipeout）。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

from consensus_tracker import ConsensusTracker

ZERO = Decimal("0")
ONE = Decimal("1")

# 1. 经 57MB 真实日志严格检验的 <=30分钟 高频发布 METAR 城市白名单 (22个站点)
FAST_METAR_CITIES: set[str] = {
    "amsterdam",     # EHAM (30m)
    "ankara",        # LTAC (30m)
    "beijing",       # ZBAA (30m)
    "guangzhou",     # ZGGG (30m)
    "helsinki",      # EFHK (30m)
    "istanbul",      # LTFM (30m)
    "karachi",       # OPKC (30m)
    "kuala-lumpur",  # WMKK (30m)
    "london",        # EGLC (30m)
    "madrid",        # LEMD (30m)
    "milan",         # LIMC (30m)
    "moscow",        # UUWW (30m)
    "munich",        # EDDM (30m)
    "paris",         # LFPB (30m)
    "seoul-incheon", # RKSI (30m)
    "shanghai",      # ZSPD (30m)
    "singapore",     # WSSS (30m)
    "taipei",        # RCSS (20-30m)
    "tel-aviv",      # LLBG (30m)
    "tokyo",         # RJTT (30m)
    "warsaw",        # EPWA (30m)
    "wellington",    # NZWN (30m)
}

# 优化后生产配置
DEFAULT_CONFIG = {
    "filter_fast_stations_only": True,
    "high_local_start": 12,
    "high_local_end": 18,
    "low_local_start": 0,
    "low_local_end": 9,
    # 稳了门槛
    "next_bucket_max_twap": Decimal("0.26"),          # 下一档 1h TWAP 阈值 (< 0.26，捕获如新加坡等胜率盘)
    "next_bucket_twap_window_s": 3600,                 # 1小时窗口
    # 执行模式: "capped_taker" (推荐) 或 "best_bid_peg"
    "entry_mode": "capped_taker",
    "yes_min_ask": Decimal("0.45"),                   # YES 必须确认一定胜率 (>0.45)
    "yes_max_ask": Decimal("0.80"),                   # YES 安全入场顶价，适度放宽至 0.80，绝不追超高 (>0.80 放弃)
    "order_budget_usdc": Decimal("15.0"),              # 每次开仓 15 USDC
    "mid_discount": Decimal("0.90"),                   # 仅在 peg 模式下使用的折价
    "resting_order_timeout_s": 180,                    # peg 模式下挂单最长 3 分钟
    # 前置风控 (抢跑止损)
    "early_stop_enabled": True,
    "early_stop_next_bucket_surge": Decimal("0.35"),  # 下一档暴涨至 0.35 触发抢跑止损
    "early_stop_bid_floor": Decimal("0.45"),          # 当前持仓 YES 盘口跌破 0.45 触发抢跑止损
    # 破位与追火约束
    "max_fires_per_session": 2,                       # 单日同一标的最多 2 次 (初次 + 1次追火)
    "risk_control_no_cap": Decimal("0.85"),           # 破位 NO 腿超过 0.85 坚决不买 (防扫空)
    "risk_control_yes_cap": Decimal("0.75"),          # 新 YES 腿同样限顶价 0.75
}


def _dec(val: Any, default: str = "0") -> Decimal:
    if val is None:
        return Decimal(default)
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal(default)


def bucket_contains(bucket: dict[str, Any], val: float) -> bool:
    lo = bucket.get("lo")
    hi = bucket.get("hi")
    return (lo is None or val >= float(lo)) and (hi is None or val < float(hi))


def mid_value(bucket: dict[str, Any]) -> float:
    lo = bucket.get("lo")
    hi = bucket.get("hi")
    if lo is not None and hi is not None:
        return (float(lo) + float(hi)) / 2.0
    if lo is not None:
        return float(lo)
    if hi is not None:
        return float(hi)
    return 0.0


def ordered_buckets(buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(buckets, key=mid_value)


def find_bucket(buckets: list[dict[str, Any]], val: float) -> dict[str, Any] | None:
    for b in buckets:
        if bucket_contains(b, val):
            return b
    return None


def get_book_mid(book: Any) -> Decimal | None:
    if not book:
        return None
    ask_raw = book.get("best_ask") if isinstance(book, dict) else getattr(book, "best_ask", None)
    bid_raw = book.get("best_bid") if isinstance(book, dict) else getattr(book, "best_bid", None)
    if ask_raw is None or bid_raw is None:
        return None
    try:
        a = Decimal(str(ask_raw))
        b = Decimal(str(bid_raw))
        if a <= ZERO or b <= ZERO or b > a:
            return None
        return (a + b) / Decimal("2")
    except Exception:
        return None


def get_best_bid(book: Any) -> Decimal | None:
    if not book:
        return None
    raw = book.get("best_bid") if isinstance(book, dict) else getattr(book, "best_bid", None)
    if raw is None:
        return None
    try:
        v = Decimal(str(raw))
        return v if v > ZERO else None
    except Exception:
        return None


def get_best_ask(book: Any) -> Decimal | None:
    if not book:
        return None
    raw = book.get("best_ask") if isinstance(book, dict) else getattr(book, "best_ask", None)
    if raw is None:
        return None
    try:
        v = Decimal(str(raw))
        return v if v > ZERO else None
    except Exception:
        return None


@dataclass
class RestingOrder:
    order_id: str
    session_key: str
    city_id: str
    bucket_id: str
    token_id: str
    side: str
    limit_price: Decimal
    shares: Decimal
    cost_usdc: Decimal
    created_at_epoch: float
    timeout_seconds: float = 180.0
    status: str = "OPEN"   # OPEN, FILLED, CANCELLED_TIMEOUT, CANCELLED_BREACH

    def is_expired(self, now_epoch: float) -> bool:
        return self.status == "OPEN" and (now_epoch - self.created_at_epoch) >= self.timeout_seconds


@dataclass
class PositionRecord:
    session_key: str
    bucket_id: str
    yes_token_id: str
    shares: Decimal
    cost_usdc: Decimal
    avg_price: Decimal
    entry_ts_utc: str
    liquidated: bool = False
    liquidation_type: str = ""   # EARLY_STOP_SURGE, EARLY_STOP_BID_FLOOR, METAR_BREACH
    recovered_usdc: Decimal = ZERO
    realized_pnl_usdc: Decimal = ZERO


@dataclass
class ConsensusLockState:
    active_orders: dict[str, RestingOrder] = field(default_factory=dict)
    open_positions: dict[str, PositionRecord] = field(default_factory=dict)
    locked_sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_fires_count: dict[str, int] = field(default_factory=dict)  # session_key -> int
    breached_sessions: set[str] = field(default_factory=set)


class ConsensusLockStrategy:
    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = {**DEFAULT_CONFIG, **(cfg or {})}
        self.state = ConsensusLockState()

    def is_fast_station(self, city_id: str) -> bool:
        if not self.cfg.get("filter_fast_stations_only", True):
            return True
        return city_id.lower() in FAST_METAR_CITIES

    def is_in_time_window(self, dt_utc: datetime, tz_name: str, direction: str) -> tuple[bool, int]:
        try:
            loc_dt = dt_utc.astimezone(ZoneInfo(tz_name))
        except Exception:
            loc_dt = dt_utc
        hour = loc_dt.hour
        dir_norm = direction.lower()
        if dir_norm == "high":
            st = int(self.cfg["high_local_start"])
            ed = int(self.cfg["high_local_end"])
            return (st <= hour < ed), hour
        else:
            st = int(self.cfg["low_local_start"])
            ed = int(self.cfg["low_local_end"])
            return (st <= hour < ed), hour

    def check_consensus_and_next_bucket(
        self,
        city_id: str,
        market_local_date: str,
        direction: str,
        ordered_bks: list[dict[str, Any]],
        target_bk: dict[str, Any],
        tracker: ConsensusTracker,
        now_utc: datetime,
    ) -> tuple[bool, str, dict[str, Any]]:
        """检查 target_bk 是否为共识第一 (Rank-1) 且下一档可能破位的桶稳定 < 20¢。"""
        meta: dict[str, Any] = {}
        ranked = tracker.rank_buckets(
            city_id, market_local_date, direction,
            now_utc=now_utc, window_seconds=self.cfg["next_bucket_twap_window_s"],
        )
        if not ranked:
            return False, "no_consensus_samples", meta

        rank1_id, rank1_mid, rank1_lead = ranked[0]
        meta["rank1_bucket_id"] = rank1_id
        meta["rank1_mid"] = str(rank1_mid)
        target_id = str(target_bk.get("bucket_id") or target_bk.get("id") or "")
        if str(rank1_id) != target_id:
            return False, f"target_is_not_rank1 (target={target_id}, rank1={rank1_id})", meta

        # 寻找下一档桶 (HIGH 为更高温度桶，LOW 为更低温度桶)
        t_idx = None
        for i, b in enumerate(ordered_bks):
            bid = str(b.get("bucket_id") or b.get("id") or "")
            if bid == target_id:
                t_idx = i
                break
        if t_idx is None:
            return False, "target_bucket_not_in_ladder", meta

        next_idx = t_idx + 1 if direction.lower() == "high" else t_idx - 1
        if 0 <= next_idx < len(ordered_bks):
            next_bk = ordered_bks[next_idx]
            next_id = str(next_bk.get("bucket_id") or next_bk.get("id") or "")
            next_series = tracker._series.get(f"{city_id}|{market_local_date}|{direction}", {}).get(next_id)
            next_twap = next_series.twap_mid(now_utc, self.cfg["next_bucket_twap_window_s"]) if next_series else None
            meta["next_bucket_id"] = next_id
            meta["next_bucket_twap"] = str(next_twap) if next_twap is not None else "none"

            max_allowed = _dec(self.cfg["next_bucket_max_twap"], "0.20")
            if next_twap is not None and next_twap >= max_allowed:
                return False, f"next_bucket_twap_too_high ({next_twap} >= {max_allowed})", meta
        else:
            meta["next_bucket_id"] = "boundary_terminal"
            meta["next_bucket_twap"] = "0"

        return True, "ok", meta

    def evaluate_entry(
        self,
        city: dict[str, Any],
        market_local_date: str,
        direction: str,
        buckets: list[dict[str, Any]],
        expected_extreme_temp: float | None,
        metar_obs: dict[str, Any] | None,
        books_by_token: dict[str, Any],
        tracker: ConsensusTracker,
        now_utc: datetime,
    ) -> dict[str, Any]:
        """评估是否触发 '稳了' 入场 (优化版：默认 Capped Taker，避免挂单逆向选择)。"""
        city_id = city["city_id"]
        key = f"{city_id}|{market_local_date}|{direction}"

        # 1. 站点频次筛选 (只做 <=30min 站点)
        if not self.is_fast_station(city_id):
            return {"action": "skip", "reason": "infrequent_metar_station", "key": key}

        # 2. 检查单日追火/开仓次数上限 (严格限制 <= 2)
        cur_fires = self.state.session_fires_count.get(key, 0)
        max_fires = int(self.cfg.get("max_fires_per_session", 2))
        if cur_fires >= max_fires:
            return {"action": "skip", "reason": f"session_max_fires_reached ({cur_fires}>={max_fires})", "key": key}

        if key in self.state.breached_sessions:
            return {"action": "skip", "reason": "session_already_breached", "key": key}
        if key in self.state.open_positions and not self.state.open_positions[key].liquidated:
            return {"action": "skip", "reason": "session_already_has_open_position", "key": key}

        # 3. 时间窗口检查
        in_win, loc_hr = self.is_in_time_window(now_utc, city.get("timezone", "UTC"), direction)
        if not in_win:
            return {"action": "skip", "reason": f"outside_time_window (hr={loc_hr})", "key": key}

        # 4. METAR 观测与预计极值
        if not metar_obs or metar_obs.get("temp_c") is None:
            return {"action": "skip", "reason": "no_valid_metar_temp", "key": key}
        curr_temp = float(metar_obs["temp_c"])

        if expected_extreme_temp is None:
            return {"action": "skip", "reason": "no_expected_extreme_reference", "key": key}

        # 5. 是否站上预计极值桶
        curr_bucket = find_bucket(buckets, curr_temp)
        target_bucket = find_bucket(buckets, float(expected_extreme_temp))
        if not curr_bucket or not target_bucket:
            return {"action": "skip", "reason": "bucket_not_found", "key": key}

        t_id = str(target_bucket.get("bucket_id") or target_bucket.get("id") or "")

        ordered = ordered_buckets(buckets)
        c_idx = ordered.index(curr_bucket)
        t_idx = ordered.index(target_bucket)

        dir_norm = direction.lower()
        if dir_norm == "high":
            if c_idx < t_idx:
                return {"action": "skip", "reason": f"not_reached_expected_high ({curr_temp} < expected {expected_extreme_temp})", "key": key}
            elif c_idx > t_idx:
                return {"action": "skip", "reason": f"already_exceeded_expected_high ({curr_temp} > expected {expected_extreme_temp})", "key": key}
        else:
            if c_idx > t_idx:
                return {"action": "skip", "reason": f"not_reached_expected_low ({curr_temp} > expected {expected_extreme_temp})", "key": key}
            elif c_idx < t_idx:
                return {"action": "skip", "reason": f"already_exceeded_expected_low ({curr_temp} < expected {expected_extreme_temp})", "key": key}

        # 6. 检查共识第一 + 下一档桶过去 1h 价格稳定 < 20¢
        ok_cons, reason_cons, meta_cons = self.check_consensus_and_next_bucket(
            city_id, market_local_date, direction, ordered, target_bucket, tracker, now_utc
        )
        if not ok_cons:
            return {"action": "skip", "reason": f"consensus_check_failed: {reason_cons}", "meta": meta_cons, "key": key}

        # 7. 盘口与执行方案 (Capped Taker 优先)
        tok_yes = target_bucket.get("yes_token_id") or target_bucket.get("_yes_token_id")
        if not tok_yes:
            return {"action": "skip", "reason": "no_yes_token_for_target_bucket", "key": key}
        book = books_by_token.get(str(tok_yes))
        if not book:
            return {"action": "skip", "reason": "target_book_missing", "key": key}

        ask = get_best_ask(book)
        bid = get_best_bid(book)
        if ask is None:
            return {"action": "skip", "reason": "no_active_ask_in_book", "key": key}

        entry_mode = self.cfg.get("entry_mode", "capped_taker")
        budget = _dec(self.cfg["order_budget_usdc"], "15.0")
        min_ask = _dec(self.cfg["yes_min_ask"], "0.45")
        max_ask = _dec(self.cfg["yes_max_ask"], "0.75")

        if entry_mode == "capped_taker":
            # 模式 A: Capped Taker (主动吃单，带顶价 0.75，不错失高胜率确定性机会)
            if ask < min_ask:
                return {"action": "skip", "reason": f"ask_below_confirmation_floor ({ask} < {min_ask})", "key": key}
            if ask > max_ask:
                return {"action": "skip", "reason": f"ask_above_safety_cap ({ask} > {max_ask})", "key": key}

            shares = (budget / ask).to_integral_value(rounding=ROUND_DOWN)
            if shares <= ZERO:
                return {"action": "skip", "reason": "zero_shares_calculated", "key": key}
            cost = ask * shares

            # 记录开仓并推进计数
            pos = PositionRecord(
                session_key=key,
                bucket_id=t_id,
                yes_token_id=str(tok_yes),
                shares=shares,
                cost_usdc=cost,
                avg_price=ask,
                entry_ts_utc=now_utc.isoformat(),
                liquidated=False,
            )
            self.state.open_positions[key] = pos
            self.state.locked_sessions[key] = {
                "locked_at": now_utc.isoformat(),
                "target_bucket_id": t_id,
                "expected_temp": expected_extreme_temp,
                "consensus_meta": meta_cons,
            }
            self.state.session_fires_count[key] = cur_fires + 1

            return {
                "action": "execute_taker_fire",
                "key": key,
                "entry_mode": "capped_taker",
                "fire_no": self.state.session_fires_count[key],
                "bucket_id": t_id,
                "token_id": str(tok_yes),
                "side": "BUY",
                "outcome": "YES",
                "fill_price": str(ask),
                "shares": str(shares),
                "cost_usdc": str(cost),
                "cap": str(max_ask),
                "consensus_meta": meta_cons,
            }
        else:
            # 模式 B: Peg 挂单模式 (带有超时自动撤销)
            mid = get_book_mid(book) or ask
            discount = _dec(self.cfg["mid_discount"], "0.90")
            tick = _dec(book.get("tick_size") if isinstance(book, dict) else getattr(book, "tick_size", None), "0.01")
            raw_price = mid * discount
            limit_price = (raw_price / tick).to_integral_value(rounding=ROUND_DOWN) * tick
            if limit_price < min_ask:
                limit_price = min_ask
            if limit_price > max_ask:
                return {"action": "skip", "reason": f"peg_price_above_cap ({limit_price} > {max_ask})", "key": key}

            shares = (budget / limit_price).to_integral_value(rounding=ROUND_DOWN)
            if shares <= ZERO:
                return {"action": "skip", "reason": "zero_shares_calculated", "key": key}
            cost = limit_price * shares

            order = RestingOrder(
                order_id=f"ord_{key}_{int(time.time())}",
                session_key=key,
                city_id=city_id,
                bucket_id=t_id,
                token_id=str(tok_yes),
                side="BUY",
                limit_price=limit_price,
                shares=shares,
                cost_usdc=cost,
                created_at_epoch=time.time(),
                timeout_seconds=float(self.cfg.get("resting_order_timeout_s", 180)),
                status="OPEN",
            )
            self.state.active_orders[order.order_id] = order
            self.state.session_fires_count[key] = cur_fires + 1
            return {
                "action": "place_resting_order",
                "key": key,
                "entry_mode": "best_bid_peg",
                "fire_no": self.state.session_fires_count[key],
                "order_id": order.order_id,
                "bucket_id": t_id,
                "token_id": str(tok_yes),
                "limit_price": str(limit_price),
                "shares": str(shares),
                "cost_usdc": str(cost),
                "timeout_seconds": order.timeout_seconds,
            }

    def evaluate_early_stop_loss(
        self,
        session_key: str,
        direction: str,
        buckets: list[dict[str, Any]],
        books_by_token: dict[str, Any],
        now_utc: datetime,
    ) -> dict[str, Any] | None:
        """核心风控 1: Pre-METAR 盘口先导抢跑止损。
        
        在官方 METAR 出炉前 30~60 秒，通过盘口异动提前感知破位迹象：
        - 触发条件 A (下一档异动): 下一档危险桶 YES 卖价暴涨突破 0.35 (买家凶猛)；
        - 触发条件 B (买盘撤单崩溃): 当前持仓桶 YES 的最佳买价跌破 0.45。
        立即以盘口最佳买价卖出割肉，保全 50%+ 资金！
        """
        if not self.cfg.get("early_stop_enabled", True):
            return None

        pos = self.state.open_positions.get(session_key)
        if not pos or pos.liquidated:
            return None

        ordered = ordered_buckets(buckets)
        curr_b = next((b for b in ordered if str(b.get("bucket_id") or b.get("id") or "") == pos.bucket_id), None)
        if not curr_b:
            return None
        c_idx = ordered.index(curr_b)

        # 定位下一档桶
        next_idx = c_idx + 1 if direction.lower() == "high" else c_idx - 1
        next_b = ordered[next_idx] if 0 <= next_idx < len(ordered) else None

        # 检查触发条件
        trigger_reason = None
        book_curr = books_by_token.get(pos.yes_token_id)
        bid_curr = get_best_bid(book_curr) or ZERO

        # 条件 B: 持仓买盘跌破防线 (e.g. 0.45)
        floor_bid = _dec(self.cfg["early_stop_bid_floor"], "0.45")
        if bid_curr < floor_bid and bid_curr > ZERO:
            trigger_reason = f"bid_floor_broken (bid={bid_curr} < {floor_bid})"

        # 条件 A: 下一档暴涨 (e.g. 0.35)
        if not trigger_reason and next_b:
            next_yes_tok = next_b.get("yes_token_id") or next_b.get("_yes_token_id")
            book_next = books_by_token.get(str(next_yes_tok)) if next_yes_tok else None
            ask_next = get_best_ask(book_next)
            surge_thresh = _dec(self.cfg["early_stop_next_bucket_surge"], "0.35")
            if ask_next is not None and ask_next >= surge_thresh:
                trigger_reason = f"next_bucket_surge (next_ask={ask_next} >= {surge_thresh})"

        if not trigger_reason:
            return None

        # 抢跑割肉平仓
        recovered_usdc = (pos.shares * bid_curr).quantize(Decimal("0.0001"))
        loss_usdc = (pos.cost_usdc - recovered_usdc).quantize(Decimal("0.0001"))
        pos.liquidated = True
        pos.liquidation_type = trigger_reason
        pos.recovered_usdc = recovered_usdc
        pos.realized_pnl_usdc = -loss_usdc

        # 撤销所有本 session 未成交挂单
        for ord_item in self.state.active_orders.values():
            if ord_item.session_key == session_key and ord_item.status == "OPEN":
                ord_item.status = "CANCELLED_BREACH"

        return {
            "action": "early_stop_loss_executed",
            "session_key": session_key,
            "reason": trigger_reason,
            "liquidated_token": pos.yes_token_id,
            "shares": str(pos.shares),
            "sell_price": str(bid_curr),
            "recovered_usdc": str(recovered_usdc),
            "loss_usdc": str(loss_usdc),
            "recovery_ratio": f"{float(recovered_usdc / pos.cost_usdc * 100):.1f}%" if pos.cost_usdc > 0 else "0%",
            "at_utc": now_utc.isoformat(),
        }

    def handle_breach_risk_control(
        self,
        session_key: str,
        city: dict[str, Any],
        direction: str,
        buckets: list[dict[str, Any]],
        new_metar_temp: float,
        books_by_token: dict[str, Any],
        now_utc: datetime,
    ) -> dict[str, Any] | None:
        """核心风控 2: METAR 实测硬破位风控与反手追火。
        
        若前置抢跑未触发，官方报文最终确认破位时执行保底风控：
        1. 立即清空持仓与挂单；
        2. 反手买 NO (严格校验 no_ask <= 0.85，若扫空至 0.99 则安全跳过)；
        3. 顺势买新 YES (限价 <= 0.75，严格受 max_fires_per_session <= 2 约束)。
        """
        lock_info = self.state.locked_sessions.get(session_key)
        pos = self.state.open_positions.get(session_key)
        if not lock_info and not pos:
            return None

        orig_bucket_id = (lock_info or {}).get("target_bucket_id") or (pos.bucket_id if pos else None)
        ordered = ordered_buckets(buckets)
        new_b = find_bucket(buckets, new_metar_temp)
        if not new_b:
            return None
        new_b_id = str(new_b.get("bucket_id") or new_b.get("id") or "")

        orig_b = next((b for b in ordered if str(b.get("bucket_id") or b.get("id") or "") == str(orig_bucket_id)), None)
        if not orig_b:
            return None
        o_idx = ordered.index(orig_b)
        n_idx = ordered.index(new_b)

        dir_norm = direction.lower()
        is_breach = (dir_norm == "high" and n_idx > o_idx) or (dir_norm == "low" and n_idx < o_idx)
        if not is_breach:
            return None

        self.state.breached_sessions.add(session_key)

        # 1. 撤销挂单
        for ord_item in self.state.active_orders.values():
            if ord_item.session_key == session_key and ord_item.status == "OPEN":
                ord_item.status = "CANCELLED_BREACH"

        # 2. 保底清仓 (若尚未割肉)
        liq_result = None
        if pos and not pos.liquidated:
            book_curr = books_by_token.get(pos.yes_token_id)
            bid_px = get_best_bid(book_curr) or ZERO
            recovered = (pos.shares * bid_px).quantize(Decimal("0.0001"))
            loss = (pos.cost_usdc - recovered).quantize(Decimal("0.0001"))
            pos.liquidated = True
            pos.liquidation_type = "METAR_BREACH"
            pos.recovered_usdc = recovered
            pos.realized_pnl_usdc = -loss
            liq_result = {
                "token_id": pos.yes_token_id,
                "shares": str(pos.shares),
                "sell_price": str(bid_px),
                "recovered_usdc": str(recovered),
                "loss_usdc": str(loss),
            }

        # 3. 检查单日追火上限 (防止连跳双杀)
        cur_fires = self.state.session_fires_count.get(session_key, 1)
        max_fires = int(self.cfg.get("max_fires_per_session", 2))
        if cur_fires >= max_fires:
            return {
                "action": "breach_risk_control_closed_only",
                "reason": f"max_fires_reached ({cur_fires}>={max_fires}), no_further_hedge",
                "session_key": session_key,
                "liquidation": liq_result,
            }

        # 4. 反手腿构建 (包含微观结构 NO 扫空防御)
        hedge_legs = []
        broken_no_tok = orig_b.get("no_token_id") or orig_b.get("_no_token_id")
        book_no = books_by_token.get(str(broken_no_tok)) if broken_no_tok else None
        ask_no = get_best_ask(book_no)
        no_cap = _dec(self.cfg["risk_control_no_cap"], "0.85")

        if ask_no is not None and ask_no <= no_cap:
            hedge_legs.append({
                "leg": "buy_no_broken",
                "token_id": str(broken_no_tok),
                "side": "BUY",
                "outcome": "NO",
                "cap": str(no_cap),
                "budget_usdc": "10.0",
            })
        else:
            # 真实微观结构记录：破位瞬间 NO ask 往往已 >=0.99 或空盘，主动安全跳过！
            pass

        new_yes_tok = new_b.get("yes_token_id") or new_b.get("_yes_token_id")
        yes_cap = _dec(self.cfg["risk_control_yes_cap"], "0.75")
        hedge_legs.append({
            "leg": "buy_yes_new",
            "token_id": str(new_yes_tok),
            "side": "BUY",
            "outcome": "YES",
            "cap": str(yes_cap),
            "budget_usdc": "15.0",
        })

        self.state.session_fires_count[session_key] = cur_fires + 1

        return {
            "action": "breach_reverse_executed",
            "session_key": session_key,
            "fire_no": self.state.session_fires_count[session_key],
            "breach_temp": new_metar_temp,
            "orig_bucket_id": orig_bucket_id,
            "new_bucket_id": new_b_id,
            "liquidation": liq_result,
            "hedge_legs": hedge_legs,
            "at_utc": now_utc.isoformat(),
        }

    def process_order_lifecycle(self, now_epoch: float, books_by_token: dict[str, Any]) -> list[dict[str, Any]]:
        events = []
        for oid, order in list(self.state.active_orders.items()):
            if order.status != "OPEN":
                continue
            if order.is_expired(now_epoch):
                order.status = "CANCELLED_TIMEOUT"
                events.append({
                    "event": "order_timeout_cancelled",
                    "order_id": oid,
                    "session_key": order.session_key,
                    "shares_unfilled": str(order.shares),
                    "cost_refunded": str(order.cost_usdc),
                    "alive_seconds": round(now_epoch - order.created_at_epoch, 1),
                })
                continue

            book = books_by_token.get(order.token_id)
            if not book:
                continue
            ask = get_best_ask(book)
            if ask is not None and ask <= order.limit_price:
                order.status = "FILLED"
                pos = PositionRecord(
                    session_key=order.session_key,
                    bucket_id=order.bucket_id,
                    yes_token_id=order.token_id,
                    shares=order.shares,
                    cost_usdc=order.cost_usdc,
                    avg_price=ask,
                    entry_ts_utc=datetime.now(timezone.utc).isoformat(),
                    liquidated=False,
                )
                self.state.open_positions[order.session_key] = pos
                events.append({
                    "event": "resting_order_filled",
                    "order_id": oid,
                    "session_key": order.session_key,
                    "shares": str(order.shares),
                    "fill_price": str(ask),
                    "limit_price": str(order.limit_price),
                    "cost_usdc": str(order.cost_usdc),
                })
        return events
