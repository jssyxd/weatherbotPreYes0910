#!/usr/bin/env python3
"""tests_consensus_lock.py — 针对优化版策略的完整测试套件 (8大微观结构场景全覆盖)"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN

from consensus_tracker import ConsensusTracker
from strategy_consensus_lock import (
    ConsensusLockStrategy,
    PositionRecord,
    RestingOrder,
)


def make_city(city_id: str = "paris", tz: str = "Europe/Paris", icao: str = "LFPB") -> dict:
    return {"city_id": city_id, "timezone": tz, "icao": icao, "market_unit": "C"}


def make_buckets() -> list[dict]:
    return [
        {"bucket_id": "b29", "lo": 29.0, "hi": 30.0, "yes_token_id": "Y29", "no_token_id": "N29"},
        {"bucket_id": "b30", "lo": 30.0, "hi": 31.0, "yes_token_id": "Y30", "no_token_id": "N30"},
        {"bucket_id": "b31", "lo": 31.0, "hi": 32.0, "yes_token_id": "Y31", "no_token_id": "N31"},
        {"bucket_id": "b32", "lo": 32.0, "hi": 33.0, "yes_token_id": "Y32", "no_token_id": "N32"},
        {"bucket_id": "b33", "lo": 33.0, "hi": 34.0, "yes_token_id": "Y33", "no_token_id": "N33"},
    ]


def test_station_filter():
    # ① 过滤器关闭（当前生产配置：操作者 2026-09-12 决定"放开全部 49 站"）⇒ 任何站都被允许
    off = ConsensusLockStrategy()
    assert off.cfg.get("filter_fast_stations_only") is False, "生产配置应为放开全部站点"
    assert off.is_fast_station("paris") is True
    assert off.is_fast_station("tokyo") is True
    assert off.is_fast_station("miami") is True       # 60 min 站在过滤器关闭后同样被允许
    assert off.is_fast_station("chicago") is True
    # ② 过滤器打开（保留语义）⇒ 仅高频站白名单通过
    on = ConsensusLockStrategy(cfg={"filter_fast_stations_only": True})
    assert on.is_fast_station("paris") is True
    assert on.is_fast_station("tokyo") is True
    assert on.is_fast_station("miami") is False       # 60 min
    assert on.is_fast_station("chicago") is False
    print("PASS: 1. test_station_filter")


def test_time_window():
    strat = ConsensusLockStrategy()
    # 14:00 UTC for Paris (UTC+2) -> 16:00 local (in 12-18 window)
    dt_in = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)
    in_win, hr = strat.is_in_time_window(dt_in, "Europe/Paris", "high")
    assert in_win is True

    # 06:00 UTC for Paris -> 08:00 local (outside 12-18 window)
    dt_out = datetime(2026, 9, 10, 6, 0, 0, tzinfo=timezone.utc)
    in_win2, hr2 = strat.is_in_time_window(dt_out, "Europe/Paris", "high")
    assert in_win2 is False
    print("PASS: 2. test_time_window")


def test_capped_taker_entry():
    """验证 Capped Taker 执行：避免挂单 0 成交陷阱，在合理区间直接吃单锁定胜率。"""
    strat = ConsensusLockStrategy({"entry_mode": "capped_taker", "yes_max_ask": Decimal("0.75")})
    city = make_city("paris")
    bks = make_buckets()
    date_str = "2026-09-10"
    dir_str = "high"
    now_utc = datetime(2026, 9, 10, 13, 0, 0, tzinfo=timezone.utc)

    tracker = ConsensusTracker()
    for t_step in range(10):
        t_sample = datetime(2026, 9, 10, 12, 10 + t_step, 0, tzinfo=timezone.utc)
        books_sample = {
            "Y31": {"best_bid": "0.65", "best_ask": "0.68", "tick_size": "0.01"},
            "Y32": {"best_bid": "0.08", "best_ask": "0.12", "tick_size": "0.01"},
        }
        tracker.record_books("paris", date_str, dir_str, bks, books_sample, t_sample)

    # 场景 A: Ask 在 0.68 (<= 0.75)，主动吃单入场，立即生成仓位
    books_now = {
        "Y31": {"best_bid": "0.65", "best_ask": "0.68", "tick_size": "0.01"},
        "Y32": {"best_bid": "0.08", "best_ask": "0.12", "tick_size": "0.01"},
    }
    obs_ok = {"temp_c": 31.2}
    res = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs_ok, books_now, tracker, now_utc)
    assert res["action"] == "execute_taker_fire"
    assert res["fill_price"] == "0.68"
    assert res["shares"] == "22"  # 15 / 0.68 = 22.05 -> 22
    assert res["fire_no"] == 1
    assert "paris|2026-09-10|high" in strat.state.open_positions

    # 场景 B: 若 Ask 涨至 0.82 (> 0.75 安全上限)，坚决不追高
    strat2 = ConsensusLockStrategy({"entry_mode": "capped_taker", "yes_max_ask": Decimal("0.75")})
    books_expensive = {
        "Y31": {"best_bid": "0.78", "best_ask": "0.82", "tick_size": "0.01"},
        "Y32": {"best_bid": "0.08", "best_ask": "0.12", "tick_size": "0.01"},
    }
    res2 = strat2.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs_ok, books_expensive, tracker, now_utc)
    assert res2["action"] == "skip"
    assert "ask_above_safety_cap" in res2["reason"]
    print("PASS: 3. test_capped_taker_entry")


def test_next_bucket_barrier_reject():
    strat = ConsensusLockStrategy()
    city = make_city("tokyo", tz="Asia/Tokyo", icao="RJTT")
    bks = make_buckets()
    date_str = "2026-09-10"
    dir_str = "high"
    now_utc = datetime(2026, 9, 10, 5, 0, 0, tzinfo=timezone.utc)

    tracker = ConsensusTracker()
    for t_step in range(10):
        t_sample = datetime(2026, 9, 10, 4, 10 + t_step, 0, tzinfo=timezone.utc)
        # 下一档 Y32 达 0.35 (>= 0.20)，说明市场预警很可能突破
        books_sample = {
            "Y31": {"best_bid": "0.55", "best_ask": "0.60"},
            "Y32": {"best_bid": "0.32", "best_ask": "0.38"},
        }
        tracker.record_books("tokyo", date_str, dir_str, bks, books_sample, t_sample)

    books_now = {
        "Y31": {"best_bid": "0.55", "best_ask": "0.60"},
        "Y32": {"best_bid": "0.32", "best_ask": "0.38"},
    }
    obs_ok = {"temp_c": 31.4}
    res = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs_ok, books_now, tracker, now_utc)
    assert res["action"] == "skip"
    assert "next_bucket_twap_too_high" in res["reason"]
    print("PASS: 4. test_next_bucket_barrier_reject")


def test_pre_metar_early_stop_next_bucket_surge():
    """验证风控亮点：在 METAR 落地前，若监测到下一档被抢买至 0.38 (>=0.35)，提前抢跑止损！"""
    strat = ConsensusLockStrategy({"early_stop_next_bucket_surge": Decimal("0.35")})
    bks = make_buckets()
    key = "paris|2026-09-10|high"
    now_utc = datetime(2026, 9, 10, 14, 15, 0, tzinfo=timezone.utc)

    # 假设我们持有 31 度 YES (买入均价 0.65，投入 14.30 USDC)
    strat.state.open_positions[key] = PositionRecord(
        session_key=key, bucket_id="b31", yes_token_id="Y31",
        shares=Decimal("22"), cost_usdc=Decimal("14.30"), avg_price=Decimal("0.65"),
        entry_ts_utc=now_utc.isoformat(),
    )

    # 盘口异动发生：32 度 YES 突然被推高到 0.38 (散户或大户抢跑买32度)
    # 此时 31 度的买盘还在 0.52 (还没彻底崩盘)
    books_surge = {
        "Y31": {"best_bid": "0.52", "best_ask": "0.58"},
        "Y32": {"best_bid": "0.34", "best_ask": "0.38"},
    }

    res = strat.evaluate_early_stop_loss(key, "high", bks, books_surge, now_utc)
    assert res is not None
    assert res["action"] == "early_stop_loss_executed"
    assert "next_bucket_surge" in res["reason"]
    # 以 0.52 成功抢跑割肉，回收 22 * 0.52 = 11.44 USDC (回收率 80%!)
    assert Decimal(res["recovered_usdc"]) == Decimal("11.44")
    assert Decimal(res["loss_usdc"]) == Decimal("2.86")  # 仅损失 2.86 而非 14.30 全亏！
    assert strat.state.open_positions[key].liquidated is True
    print("PASS: 5. test_pre_metar_early_stop_next_bucket_surge (成功保全 80% 本金)")


def test_pre_metar_early_stop_bid_floor():
    """验证风控亮点：持仓桶买盘撤单跌破 0.45，立即自动割肉保命。"""
    strat = ConsensusLockStrategy({"early_stop_bid_floor": Decimal("0.45")})
    bks = make_buckets()
    key = "paris|2026-09-10|high"
    now_utc = datetime(2026, 9, 10, 14, 20, 0, tzinfo=timezone.utc)

    strat.state.open_positions[key] = PositionRecord(
        session_key=key, bucket_id="b31", yes_token_id="Y31",
        shares=Decimal("22"), cost_usdc=Decimal("14.30"), avg_price=Decimal("0.65"),
        entry_ts_utc=now_utc.isoformat(),
    )

    # 买盘突然从 0.65 溃退到 0.40
    books_collapse = {
        "Y31": {"best_bid": "0.40", "best_ask": "0.48"},
        "Y32": {"best_bid": "0.20", "best_ask": "0.25"},
    }
    res = strat.evaluate_early_stop_loss(key, "high", bks, books_collapse, now_utc)
    assert res is not None
    assert "bid_floor_broken" in res["reason"]
    assert Decimal(res["recovered_usdc"]) == Decimal("8.80")
    print("PASS: 6. test_pre_metar_early_stop_bid_floor")


def test_breach_risk_control_and_no_defence():
    """验证实测硬破位风控：微观结构 NO 扫空防御机制。"""
    strat = ConsensusLockStrategy({"risk_control_no_cap": Decimal("0.85")})
    city = make_city("paris")
    bks = make_buckets()
    key = "paris|2026-09-10|high"
    now_utc = datetime(2026, 9, 10, 14, 30, 0, tzinfo=timezone.utc)

    strat.state.open_positions[key] = PositionRecord(
        session_key=key, bucket_id="b31", yes_token_id="Y31",
        shares=Decimal("22"), cost_usdc=Decimal("14.30"), avg_price=Decimal("0.65"),
        entry_ts_utc=now_utc.isoformat(),
    )
    strat.state.session_fires_count[key] = 1

    # 官方 METAR 证实跳到 32.5 度 (破位!)
    # 实盘微观结构：N31 ask 已被扫至 0.99，Y32 ask 为 0.70
    books_swept = {
        "Y31": {"best_bid": "0.01", "best_ask": "0.05"},
        "N31": {"best_bid": "0.95", "best_ask": "0.99"},  # NO 昂贵超限
        "Y32": {"best_bid": "0.65", "best_ask": "0.70"},
    }

    res = strat.handle_breach_risk_control(key, city, "high", bks, 32.5, books_swept, now_utc)
    assert res["action"] == "breach_reverse_executed"
    # 验证防御：N31 超过 0.85 被安全跳过，只保留有安全边际的 Y32
    legs = {l["leg"]: l for l in res["hedge_legs"]}
    assert "buy_no_broken" not in legs, "NO leg above 0.85 must be skipped"
    assert "buy_yes_new" in legs and legs["buy_yes_new"]["token_id"] == "Y32"
    assert strat.state.session_fires_count[key] == 2
    print("PASS: 7. test_breach_risk_control_and_no_defence")


def test_max_fires_cap_enforcement():
    """验证单日双火硬顶：同一 Session 最多 2 次开仓/反手，杜绝 Warsaw 式连环双杀。"""
    strat = ConsensusLockStrategy({"max_fires_per_session": 2})
    city = make_city("warsaw", tz="Europe/Warsaw", icao="EPWA")
    bks = make_buckets()
    key = "warsaw|2026-09-10|high"
    now_utc = datetime(2026, 9, 10, 15, 0, 0, tzinfo=timezone.utc)

    # 已经发生过 2 次开仓
    strat.state.session_fires_count[key] = 2
    strat.state.open_positions[key] = PositionRecord(
        session_key=key, bucket_id="b32", yes_token_id="Y32",
        shares=Decimal("20"), cost_usdc=Decimal("14.0"), avg_price=Decimal("0.70"),
        entry_ts_utc=now_utc.isoformat(),
    )

    # 温度再次突破到 33.5 度 (第三次破位)
    books = {"Y32": {"best_bid": "0.01"}, "Y33": {"best_bid": "0.70"}}
    res = strat.handle_breach_risk_control(key, city, "high", bks, 33.5, books, now_utc)
    assert res["action"] == "breach_risk_control_closed_only"
    assert "max_fires_reached" in res["reason"]
    # 绝不再追买第三手，只清空持仓止损，锁死账户单日最大回撤！
    print("PASS: 8. test_max_fires_cap_enforcement")


def test_post_stop_loss_cooldown():
    """防线 1: 验证止损后单向熔断冷却，彻底杜绝赫尔辛基式同一分钟连续开仓。"""
    strat = ConsensusLockStrategy({"early_stop_next_bucket_surge": Decimal("0.35")})
    city = make_city("helsinki", tz="Europe/Helsinki", icao="EFHK")
    bks = make_buckets()
    key = "helsinki|2026-09-10|high"
    now_utc = datetime(2026, 9, 10, 12, 40, 27, tzinfo=timezone.utc)

    # 1. 模拟初次开仓并持仓
    strat.state.open_positions[key] = PositionRecord(
        session_key=key, bucket_id="b31", yes_token_id="Y31",
        shares=Decimal("18.75"), cost_usdc=Decimal("14.43"), avg_price=Decimal("0.77"),
        entry_ts_utc=now_utc.isoformat(),
    )
    strat.state.session_fires_count[key] = 1

    # 2. 下一档暴涨至 0.49 触发提前止损
    books_surge = {
        "Y31": {"best_bid": "0.50", "best_ask": "0.60"},
        "Y32": {"best_bid": "0.45", "best_ask": "0.49"},
    }
    stop_res = strat.evaluate_early_stop_loss(key, "high", bks, books_surge, now_utc)
    assert stop_res is not None
    assert key in strat.state.stopped_out_sessions
    assert key in strat.state.breached_sessions

    # 3. 验证紧接着的下一个 tick 即使 session_fires_count=1 < max_fires=2，也坚决拒绝二次入场！
    tracker = ConsensusTracker()
    tracker.record_books("helsinki", "2026-09-10", "high", bks, books_surge, now_utc)
    obs = {"temp_c": 31.0, "obs_age_s": 0}
    entry_res = strat.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_surge, tracker, now_utc)
    assert entry_res["action"] == "skip"
    assert entry_res["reason"] == "session_already_stopped_out"
    print("PASS: 9. test_post_stop_loss_cooldown (止损后单向熔断成功阻断二次入场)")


def test_temperature_velocity_stalling_filter():
    """防线 2: 验证气温变化率停滞过滤器 (冲顶动量拦截与见顶企稳放行)。"""
    strat = ConsensusLockStrategy({"min_dwell_seconds_if_rising": 1800})
    city = make_city("helsinki", tz="Europe/Helsinki", icao="EFHK")
    bks = make_buckets()
    date_str = "2026-09-10"
    dir_str = "high"
    t0 = datetime(2026, 9, 10, 12, 10, 0, tzinfo=timezone.utc)

    tracker = ConsensusTracker()
    books = {
        "Y31": {"best_bid": "0.65", "best_ask": "0.70"},
        "Y32": {"best_bid": "0.05", "best_ask": "0.10"},
    }
    tracker.record_books("helsinki", date_str, dir_str, bks, books, t0)

    # 步骤 1: 上一份报文为 30.0 度
    obs1 = {"temp_c": 30.0, "obs_age_s": 600}
    res1 = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs1, books, tracker, t0)
    assert res1["action"] == "skip"
    assert "not_reached_expected_high" in res1["reason"]

    # 步骤 2: 下一份报文跳升到 31.0 度 (刚跳字，升温斜率冲顶，停留仅 60 秒)
    t1 = t0 + timedelta(minutes=10)
    obs2 = {"temp_c": 31.0, "obs_age_s": 60}
    res2 = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs2, books, tracker, t1)
    assert res2["action"] == "skip"
    assert "temperature_rising_velocity_active" in res2["reason"]
    print("PASS: 10a. test_temperature_velocity_stalling_filter (刚跳升未停滞成功拦截)")

    # 步骤 3: 维持该温度超过 1800 秒 (见顶企稳，斜率归零)
    t2 = t1 + timedelta(seconds=1900)
    obs3 = {"temp_c": 31.0, "obs_age_s": 1900}
    res3 = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs3, books, tracker, t2)
    assert res3["action"] == "execute_taker_fire"
    print("PASS: 10b. test_temperature_velocity_stalling_filter (充分停滞后正常放行开仓)")


def test_next_bucket_instantaneous_book_checks():
    """防线 3: 验证下一档瞬时盘口与买单防御 (防范快钱突袭与 TWAP 滞后)。"""
    strat = ConsensusLockStrategy({
        "next_bucket_max_twap": Decimal("0.26"),
        "next_bucket_max_instant_ask": Decimal("0.25"),
        "next_bucket_max_instant_bid": Decimal("0.15"),
    })
    city = make_city("tokyo", tz="Asia/Tokyo", icao="RJTT")
    bks = make_buckets()
    date_str = "2026-09-10"
    dir_str = "high"
    now_utc = datetime(2026, 9, 10, 5, 0, 0, tzinfo=timezone.utc)

    # 过去 1 小时 TWAP 看起来很低 (0.15 < 0.26)
    tracker = ConsensusTracker()
    for t_step in range(10):
        t_sample = datetime(2026, 9, 10, 4, 10 + t_step, 0, tzinfo=timezone.utc)
        books_sample = {
            "Y31": {"best_bid": "0.65", "best_ask": "0.70"},
            "Y32": {"best_bid": "0.10", "best_ask": "0.15"},
        }
        tracker.record_books("tokyo", date_str, dir_str, bks, books_sample, t_sample)

    # 场景 A: 瞬时 Ask 突增至 0.28 (>= 0.25)
    books_instant_ask_high = {
        "Y31": {"best_bid": "0.65", "best_ask": "0.70"},
        "Y32": {"best_bid": "0.10", "best_ask": "0.28"},
    }
    obs = {"temp_c": 31.0, "obs_age_s": 2000}
    res_a = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs, books_instant_ask_high, tracker, now_utc)
    assert res_a["action"] == "skip"
    assert "next_bucket_instant_ask_too_high" in res_a["reason"]

    # 场景 B: 瞬时 Bid 潜伏至 0.18 (>= 0.15)
    books_instant_bid_high = {
        "Y31": {"best_bid": "0.65", "best_ask": "0.70"},
        "Y32": {"best_bid": "0.18", "best_ask": "0.22"},
    }
    res_b = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs, books_instant_bid_high, tracker, now_utc)
    assert res_b["action"] == "skip"
    assert "next_bucket_instant_bid_too_high" in res_b["reason"]
    print("PASS: 11. test_next_bucket_instantaneous_book_checks (瞬时盘口双向校验生效)")


# --------------------------------------------------------------------------- #
# 并行通道：下一档桶廉价入场 (buy_yes_next)  —— 2026-09-12
# 依据 preyes_param_sim_20260912.md §5/§6：既有通道的非价格门全通过时目标桶 ask 已被
# 定价到 0.81–0.99（cap 0.75 挡死 ⇒ 8h 零成交）⇒ 改入场对象为"下一档桶"，窗口自有独立。
# --------------------------------------------------------------------------- #
NEXT_CFG = {
    "next_entry_enabled": True,
    "next_entry_min_ask": "0.20",
    "next_entry_max_ask": "0.32",
    "next_entry_budget_pct": "0.5",
    "order_budget_usdc": "15.0",
}


def _next_entry_case(ask_next: str, *, ask_target: str = "0.90", cfg_extra: dict | None = None,
                     enabled: bool = True, samples: bool = True, now_utc=None, city=None,
                     temp: float = 31.2, obs_age_s: float = 2000.0, expected: float | None = 31.0,
                     date_str: str = "2026-09-10", dir_str: str = "high",
                     next_sample_ask: str = "0.12"):
    """一个"非价格门全通过 + 给定下一档桶 ask"的场景（新通道测试用）。

    `ask_target` 默认 0.90 = 实盘观测到的目标桶报价（被既有 cap 0.75 挡死）；`ask_next` 是新通道
    唯一的变量；`next_sample_ask` 决定 tracker 里下一档桶的历史 TWAP（0.12 ⇒ ~0.10，低于既有
    0.26 门）。返回 (strat, res, tracker, now, city, books)。
    """
    cfg = dict(NEXT_CFG) if enabled else dict(NEXT_CFG, next_entry_enabled=False)
    cfg.update(cfg_extra or {})
    strat = ConsensusLockStrategy(cfg)
    city = city or make_city("paris")
    bks = make_buckets()                     # b29..b33 (Y29..Y33)；target=b31，next=b32
    tracker = ConsensusTracker()
    if samples:
        next_bid = str(Decimal(next_sample_ask) - Decimal("0.02"))
        for step in range(10):
            ts = datetime(2026, 9, 10, 12, 10 + step, 0, tzinfo=timezone.utc)
            tracker.record_books("paris", date_str, dir_str, bks, {
                "Y31": {"best_bid": "0.65", "best_ask": "0.68"},
                "Y32": {"best_bid": next_bid, "best_ask": next_sample_ask},
            }, ts)
    now = now_utc or datetime(2026, 9, 10, 13, 0, 0, tzinfo=timezone.utc)   # Paris 15:00 本地
    books = {
        "Y31": {"best_bid": "0.80", "best_ask": ask_target, "tick_size": "0.01"},
        "Y32": {"best_bid": "0.10", "best_ask": ask_next, "tick_size": "0.01"},
    }
    obs = {"temp_c": temp, "obs_age_s": obs_age_s}
    res = strat.evaluate_entry(city, date_str, dir_str, bks, expected, obs, books, tracker, now)
    return strat, res, tracker, now, city, books


def test_next_entry_channel_default_off_and_config_gate():
    """① 代码默认关闭（保守）；② 部署 config 显式开启；③ 非法窗口 fail-closed 不回退。"""
    default = ConsensusLockStrategy()
    assert default.cfg["next_entry_enabled"] is False, "代码默认必须是关闭"
    win = default.next_entry_window()
    assert win["ok"] is True and str(win["lo"]) == "0.20" and str(win["hi"]) == "0.32"
    assert win["label"] == "(0.20, 0.32]"

    # 关闭态：即使下一档桶 ask=0.25 在窗口内，也必须走既有通道（被既有共识/顶价门挡下）
    strat_off, res_off, *_ = _next_entry_case("0.25", enabled=False)
    assert res_off["action"] == "skip", res_off
    assert ("ask_above_safety_cap" in res_off["reason"]
            or "next_bucket_instant_ask_too_high" in res_off["reason"]), res_off
    assert strat_off.last_next_entry_skip is None, "未评估的新通道不留审计噪声"
    assert "paris|2026-09-10|high" not in strat_off.state.open_positions

    # 开启态：同一场景由新通道成交（证明 window 是唯一的开关差异）
    strat_on, res_on, *_ = _next_entry_case("0.25", enabled=True)
    assert res_on["action"] == "execute_taker_fire"
    assert res_on["entry_channel"] == "next_bucket"
    assert res_on["bucket_id"] == "b32" and res_on["token_id"] == "Y32"

    # 明确的语义替代（有意为之，报告里如实记录）：下一档桶的 twap/instant **价格子门**
    # （0.26/0.25/0.15）是既有目标桶通道的过滤器；新通道用**自有窗口** (0.20, 0.32] 替代它，
    # 因此 twap>=0.26 时新通道仍可入场 —— 但 ask 必须落在窗口内（见边界矩阵：0.321 仍弃单）。
    strat_sub, res_sub, *_ = _next_entry_case("0.25", next_sample_ask="0.41")
    assert float(res_sub["consensus_meta"]["next_bucket_twap"]) >= 0.26, res_sub
    assert res_sub["action"] == "execute_taker_fire" and res_sub["entry_channel"] == "next_bucket", res_sub
    # 同一 fixture、通道关闭 ⇒ 既有通道被 twap 价格子门挡死（证明这里确实是"替代"而非"绕过"）
    _, res_sub_off, *_ = _next_entry_case("0.25", next_sample_ask="0.41", enabled=False)
    assert res_sub_off["action"] == "skip" and "next_bucket_twap_too_high" in res_sub_off["reason"], res_sub_off

    # 非法窗口（lo >= hi）⇒ 整通道弃单，绝不回退既有 (0.45, 0.75]：
    # 下一档 0.20 让既有通道的共识门通过 ⇒ 若新通道错误回退窗口，成交来源就会变成 next_bucket
    strat_bad, res_bad, *_ = _next_entry_case(
        "0.20", ask_target="0.60", cfg_extra={"next_entry_min_ask": "0.40", "next_entry_max_ask": "0.32"})
    assert strat_bad.last_next_entry_skip and "next_entry_window_invalid" in strat_bad.last_next_entry_skip
    assert res_bad["action"] == "execute_taker_fire", res_bad
    assert res_bad["entry_channel"] == "target_bucket" and res_bad["bucket_id"] == "b31", \
        "非法窗口时只能是既有通道按自己的窗口成交（绝不回退成新通道窗口）"
    for bad_lo, bad_hi in (("abc", "0.32"), ("0.20", "NaN"), ("-0.1", "0.32"), ("0.20", "1.5"), ("0.3", "0.3")):
        w = ConsensusLockStrategy({"next_entry_min_ask": bad_lo, "next_entry_max_ask": bad_hi}).next_entry_window()
        assert w["ok"] is False, (bad_lo, bad_hi, w)
    print("PASS: 12. test_next_entry_channel_default_off_and_config_gate "
          "(默认关闭 / config 开启 / 非法窗口 fail-closed 不回退)")


def test_next_entry_window_boundary_matrix():
    """窗口边界矩阵 (0.20, 0.32]：0.199/0.200/0.201/0.319/0.320/0.321 ⇒ 弃/弃/入/入/入/弃。"""
    probes = [("0.199", "skip"), ("0.200", "skip"), ("0.201", "fire"),
              ("0.319", "fire"), ("0.320", "fire"), ("0.321", "skip")]
    print("  [next-entry window boundary matrix] window = (0.20, 0.32]  (half-open)")
    for raw, want in probes:
        strat, res, *_ = _next_entry_case(raw)
        got = "fire" if res.get("action") == "execute_taker_fire" else "skip"
        skip_reason = strat.last_next_entry_skip
        print(f"    next_ask={raw:>6} -> {got:4}  channel={res.get('entry_channel') or 'n/a':>12}"
              f"  next_entry_skip={skip_reason}")
        assert got == want, (raw, want, res)
        if want == "fire":
            assert res["entry_channel"] == "next_bucket" and res["bucket_id"] == "b32", res
            assert res["floor"] == "0.20" and res["cap"] == "0.32", res
            assert res["window"] == "(0.20, 0.32]" and res["next_entry_window"] == "(0.20, 0.32]", res
            assert Decimal(res["fill_price"]) == Decimal(raw), res
            assert Decimal(res["shares"]) * Decimal(res["fill_price"]) <= Decimal("7.50"), res
            assert Decimal(res["shares"]) == (Decimal("7.50") / Decimal(raw)).to_integral_value(
                rounding=ROUND_DOWN), res
        else:
            expect = "next_entry_ask_below_window" if Decimal(raw) <= Decimal("0.20") \
                else "next_entry_ask_above_window"
            assert skip_reason and expect in skip_reason, (raw, skip_reason)
            # 弃单 ⇒ 新通道没有任何仓位/额度占用
            assert strat.state.session_next_entry_used.get("paris|2026-09-10|high") is None
    # 半开端点的精确复核：0.20 本身弃单、0.32 本身入场
    s1, r1, *_ = _next_entry_case("0.2")
    assert r1["action"] == "skip" and "next_entry_ask_below_window" in (s1.last_next_entry_skip or "")
    s2, r2, *_ = _next_entry_case("0.32")
    assert r2["action"] == "execute_taker_fire", r2
    # 窗口是自有且独立的：改窗口 ⇒ 边界随之移动
    s3, r3, *_ = _next_entry_case("0.26", cfg_extra={"next_entry_min_ask": "0.28"})
    assert r3["action"] == "skip" and "next_entry_ask_below_window" in (s3.last_next_entry_skip or "")
    print("PASS: 13. test_next_entry_window_boundary_matrix (半开窗口 6 组边界全部符合预期)")


def test_next_entry_priority_and_budget_isolation():
    """通道优先级 + 预算隔离：新通道先评估；命中则既有通道不下单；两者预算永不重叠。"""
    key = "paris|2026-09-10|high"
    # (a) 两个通道同时可成交（目标桶 0.60 ∈ (0.45,0.75]；下一档 0.25 ∈ (0.20,0.32]）
    strat, res, *_ = _next_entry_case("0.25", ask_target="0.60")
    assert res["action"] == "execute_taker_fire", res
    assert res["entry_channel"] == "next_bucket", "新通道必须优先，既有通道本次不得下单"
    assert res["bucket_id"] == "b32" and res["token_id"] == "Y32", res
    assert Decimal(res["shares"]) == Decimal("30")            # floor(7.5 / 0.25)
    assert Decimal(res["cost_usdc"]) == Decimal("7.50")
    assert res["budget_usdc"] == "7.50" and res["budget_pct"] == "0.5"
    assert strat.state.session_next_entry_used[key] == Decimal("7.50")
    # 既有通道只剩剩余额度（7.5 已被新通道占用 ⇒ 15 − 7.5 = 7.5）
    assert strat.target_channel_budget(key, Decimal("15")) == Decimal("7.50")
    assert Decimal(res["cost_usdc"]) <= Decimal("15")         # 总名义额 ≤ fire 预算
    # 会话上限沿用既有语义：同会话已有持仓 ⇒ 两个通道都不再 fire
    res_again = strat.evaluate_entry(
        make_city("paris"), "2026-09-10", "high", make_buckets(), 31.0,
        {"temp_c": 31.2, "obs_age_s": 2000}, {
            "Y31": {"best_bid": "0.80", "best_ask": "0.60"},
            "Y32": {"best_bid": "0.10", "best_ask": "0.25"}}, ConsensusTracker(),
        datetime(2026, 9, 10, 13, 5, 0, tzinfo=timezone.utc))
    assert res_again["action"] == "skip" and res_again["reason"] == "session_already_has_open_position"

    # (b) 新通道弃单（下一档 0.20 == 窗口下界，半开区间不含）⇒ 既有通道按**原窗口**独立判定并成交
    strat2, res2, *_ = _next_entry_case("0.20", ask_target="0.60")
    assert strat2.last_next_entry_skip and "next_entry_ask_below_window" in strat2.last_next_entry_skip
    assert res2["action"] == "execute_taker_fire", res2
    assert res2["entry_channel"] == "target_bucket" and res2["bucket_id"] == "b31", res2
    assert res2["cap"] == "0.75", res2                       # 既有窗口一字未改
    assert Decimal(res2["shares"]) == Decimal("25")          # floor(15 / 0.60) ⇒ 吃满 fire 预算
    assert strat2.target_channel_budget(key, Decimal("15")) == Decimal("15")
    assert strat2.state.session_next_entry_used.get(key) is None, "弃单不得占用任何额度"
    assert Decimal(res2["cost_usdc"]) <= Decimal("15")

    # (c) 下一档 ask 高于窗口（0.40）⇒ 新通道弃单，且任何成交都不可能是下一档桶
    strat3, res3, *_ = _next_entry_case("0.40", ask_target="0.60")
    assert strat3.last_next_entry_skip and "next_entry_ask_above_window" in strat3.last_next_entry_skip
    assert res3.get("entry_channel") != "next_bucket", res3
    assert strat3.state.session_next_entry_used.get(key) is None
    print("PASS: 14. test_next_entry_priority_and_budget_isolation "
          "(新通道优先 / 既有通道独立 / 15−7.5=7.5 剩余额度)")


def test_next_entry_non_price_gates_not_relaxed():
    """非价格门一个都不放松：站点/时间窗/预期极值/变率/共识 rank1 任一不过 ⇒ 新通道也不 fire。"""
    key = "paris|2026-09-10|high"
    # ① 站点频次门（filter_fast_stations_only=True 时非白名单站点被挡）
    strat, res, *_ = _next_entry_case("0.25", city=make_city("testville", "UTC", "TEST"),
                                      cfg_extra={"filter_fast_stations_only": True})
    assert res["action"] == "skip" and res["reason"] == "infrequent_metar_station", res
    assert res.get("entry_channel") != "next_bucket"

    # ② 时间窗门（Paris 06:00Z = 08:00 本地，不在 14–18）
    strat, res, *_ = _next_entry_case("0.25", now_utc=datetime(2026, 9, 10, 6, 0, 0, tzinfo=timezone.utc))
    assert res["action"] == "skip" and "outside_time_window" in res["reason"], res

    # ③ 预期极值桶位门（气温还没到预期极值）
    strat, res, *_ = _next_entry_case("0.25", temp=30.2)
    assert res["action"] == "skip" and "not_reached_expected_high" in res["reason"], res

    # ④ 速度/变率门（刚跳升未停滞）
    strat = ConsensusLockStrategy(NEXT_CFG)
    city = make_city("helsinki", "Europe/Helsinki", "EFHK")
    bks = make_buckets()
    tracker = ConsensusTracker()
    tracker.record_books("helsinki", "2026-09-10", "high", bks, {
        "Y31": {"best_bid": "0.65", "best_ask": "0.70"}, "Y32": {"best_bid": "0.08", "best_ask": "0.12"}},
        datetime(2026, 9, 10, 12, 10, 0, tzinfo=timezone.utc))
    books = {"Y31": {"best_bid": "0.80", "best_ask": "0.90"},
             "Y32": {"best_bid": "0.10", "best_ask": "0.25"}}
    t0 = datetime(2026, 9, 10, 12, 10, 0, tzinfo=timezone.utc)
    strat.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, {"temp_c": 30.0, "obs_age_s": 600},
                         books, tracker, t0)
    t1 = t0 + timedelta(minutes=10)
    res = strat.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, {"temp_c": 31.0, "obs_age_s": 60},
                               books, tracker, t1)
    assert res["action"] == "skip" and "temperature_rising_velocity_active" in res["reason"], res
    assert res.get("entry_channel") != "next_bucket"

    # ⑤ 共识 rank1 门（rank1 是另一个桶 ⇒ 新通道同样拒绝，哪怕下一档 ask 在窗口内）
    strat = ConsensusLockStrategy(NEXT_CFG)
    tracker = ConsensusTracker()
    for step in range(10):
        ts = datetime(2026, 9, 10, 12, 10 + step, 0, tzinfo=timezone.utc)
        tracker.record_books("paris", "2026-09-10", "high", bks, {
            "Y31": {"best_bid": "0.10", "best_ask": "0.12"},       # target 便宜（非 rank1）
            "Y32": {"best_bid": "0.65", "best_ask": "0.70"},       # 别的桶才是 rank1
        }, ts)
    res = strat.evaluate_entry(make_city("paris"), "2026-09-10", "high", bks, 31.0,
                               {"temp_c": 31.2, "obs_age_s": 2000}, {
                                   "Y31": {"best_bid": "0.10", "best_ask": "0.60"},
                                   "Y32": {"best_bid": "0.10", "best_ask": "0.25"}},
                               tracker, datetime(2026, 9, 10, 13, 0, 0, tzinfo=timezone.utc))
    assert res["action"] == "skip" and "target_is_not_rank1" in res["reason"], res
    assert res.get("entry_channel") != "next_bucket"
    assert strat.last_next_entry_skip and "target_is_not_rank1" in strat.last_next_entry_skip
    assert key not in strat.state.open_positions
    print("PASS: 15. test_next_entry_non_price_gates_not_relaxed "
          "(站点/时间窗/极值桶位/变率/共识 rank1 五门全部仍然拦截)")


def main():
    test_station_filter()
    test_time_window()
    test_capped_taker_entry()
    test_next_bucket_barrier_reject()
    test_pre_metar_early_stop_next_bucket_surge()
    test_pre_metar_early_stop_bid_floor()
    test_breach_risk_control_and_no_defence()
    test_max_fires_cap_enforcement()
    test_post_stop_loss_cooldown()
    test_temperature_velocity_stalling_filter()
    test_next_bucket_instantaneous_book_checks()
    test_next_entry_channel_default_off_and_config_gate()
    test_next_entry_window_boundary_matrix()
    test_next_entry_priority_and_budget_isolation()
    test_next_entry_non_price_gates_not_relaxed()
    print("\nALL 15 OPTIMIZATION UNIT TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
