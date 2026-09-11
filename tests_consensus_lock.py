#!/usr/bin/env python3
"""tests_consensus_lock.py — 针对优化版策略的完整测试套件 (8大微观结构场景全覆盖)"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal

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
    strat = ConsensusLockStrategy()
    assert strat.is_fast_station("paris") is True
    assert strat.is_fast_station("tokyo") is True
    assert strat.is_fast_station("miami") is False  # 60 min
    assert strat.is_fast_station("chicago") is False
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
    print("\nALL 11 OPTIMIZATION UNIT TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
