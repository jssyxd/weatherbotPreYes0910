"""tests_cycle_consensus_lock.py — Integration test for consensus_lock execution in _r_cycle.py"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, ".")
import _r_cycle
from paper_capital import initial_capital_usdc, remaining_capital_usdc

def test_paper_fire_buy_yes_lock():
    cfg = {
        "mode": "paper",
        "strategy_mode": "consensus_lock",
        "fire_budget_usdc": 15.0,
        "log_path": "data/test_events.jsonl",
    }
    state = {
        "paper_initial_capital_usdc": 700.0,
        "positions": {},
        "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {}, "last_obs_time": {}},
    }
    now = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)
    
    # Mock orderbook in cache
    tok_id = "YES-PARIS-31"
    _r_cycle.book_cache()[tok_id] = {
        "best_ask": "0.60",
        "best_bid": "0.58",
        "tick_size": "0.01",
        "asks": [{"price": "0.60", "size": "100"}],
        "bids": [{"price": "0.58", "size": "100"}],
    }

    fire = {
        "key": "paris|2026-09-10|high",
        "kind": "consensus_lock",
        "city_id": "paris",
        "icao": "LFPB",
        "market_local_date": "2026-09-10",
        "direction": "high",
        "ref_extreme": 31.0,
        "ref_source": "market_rank1",
        "running_extreme": 31.2,
        "jump": 0,
        "target_bucket_id": "b31",
        "local_fire_time": "2026-09-10T16:00:00+02:00",
        "market_unit": "C",
        "fire_no": 1,
        "budget_usdc": "15.0",
        "legs": [
            {
                "leg": "buy_yes_lock",
                "token_id": tok_id,
                "side": "BUY",
                "outcome": "YES",
                "cap": "0.75",
                "floor": "0.45",
                "notional_pct": "1.0",
                "bucket_id": "b31",
                "bucket_lo": 31.0,
                "bucket_hi": 32.0,
                "bucket_label": "31°C",
            }
        ],
        "action_type": "re_fire",
    }

    pos, ladlog = _r_cycle._paper_fire(cfg, state, fire, now)
    assert pos is not None, "Position should be filled"
    assert pos["kind"] == "consensus_lock"
    assert len(pos["legs"]) == 1
    leg = pos["legs"][0]
    assert leg["leg"] == "buy_yes_lock"
    assert leg["token_id"] == tok_id
    assert leg["bucket_id"] == "b31"
    assert Decimal(leg["shares"]) > 0
    assert Decimal(leg["cost_usdc"]) > 0
    
    # Record fire event into state
    _r_cycle._record_fire_event(cfg, state, fire, pos, ladlog, now)
    assert "paris|2026-09-10|high" in state["positions"]
    print("PASS: test_paper_fire_buy_yes_lock")


def test_early_stop_loss_liquidation():
    cfg = {
        "mode": "paper",
        "strategy_mode": "consensus_lock",
        "fire_budget_usdc": 15.0,
        "log_path": "data/test_events.jsonl",
        "consensus_lock": {
            "early_stop_enabled": True,
            "early_stop_next_bucket_surge": "0.35",
            "early_stop_bid_floor": "0.45",
        }
    }
    state = {
        "paper_initial_capital_usdc": 700.0,
        "paper_total_debit_usdc": 15.0,
        "positions": {
            "paris|2026-09-10|high": {
                "key": "paris|2026-09-10|high",
                "kind": "consensus_lock",
                "city_id": "paris",
                "direction": "high",
                "settled": False,
                "legs": [
                    {
                        "leg": "buy_yes_lock",
                        "token_id": "YES-PARIS-31",
                        "side": "BUY",
                        "outcome": "YES",
                        "shares": "25.0",
                        "cost_usdc": "15.0",
                        "avg_price": "0.60",
                        "bucket_id": "b31",
                        "settled": False,
                    }
                ]
            }
        },
        "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {}, "last_obs_time": {}},
    }
    
    # Simulated books where next bucket surges to 0.40 (>0.35 threshold)
    books = {
        "YES-PARIS-31": {"best_bid": "0.50", "best_ask": "0.55"},
        "YES-PARIS-32": {"best_bid": "0.38", "best_ask": "0.40"},
    }
    buckets = [
        {"bucket_id": "b30", "lo": 30.0, "hi": 31.0, "yes_token_id": "YES-PARIS-30"},
        {"bucket_id": "b31", "lo": 31.0, "hi": 32.0, "yes_token_id": "YES-PARIS-31"},
        {"bucket_id": "b32", "lo": 32.0, "hi": 33.0, "yes_token_id": "YES-PARIS-32"},
    ]
    
    strat = _r_cycle._get_consensus_lock_strat(cfg)
    now = datetime(2026, 9, 10, 14, 5, 0, tzinfo=timezone.utc)
    
    # Sync strat state
    strat.state.open_positions["paris|2026-09-10|high"] = _r_cycle.PositionRecord(
        session_key="paris|2026-09-10|high",
        bucket_id="b31",
        yes_token_id="YES-PARIS-31",
        shares=Decimal("25.0"),
        cost_usdc=Decimal("15.0"),
        avg_price=Decimal("0.60"),
        entry_ts_utc=now.isoformat(),
        liquidated=False,
    )
    
    early_stop = strat.evaluate_early_stop_loss(
        "paris|2026-09-10|high", "high", buckets, books, now
    )
    assert early_stop is not None
    assert "next_bucket_surge" in early_stop["reason"]

    # Close leg via close_leg_at_best_bid
    pos = state["positions"]["paris|2026-09-10|high"]
    leg = pos["legs"][0]
    res = _r_cycle.close_leg_at_best_bid(state, leg, books, closed_by="early_stop_loss")
    assert res is not None
    assert Decimal(res["proceeds_usdc"]) == Decimal("12.5000")  # 25 shares * 0.50 bid = 12.5 USDC
    # debit reduced from 15.0 to 2.5 (12.5 credited back)
    assert round(float(state["paper_total_debit_usdc"]), 4) == 2.5
    print("PASS: test_early_stop_loss_liquidation (12.5 / 15.0 = 83.3% capital preserved)")


if __name__ == "__main__":
    test_paper_fire_buy_yes_lock()
    test_early_stop_loss_liquidation()
    print("ALL INTEGRATION TESTS PASSED!")
