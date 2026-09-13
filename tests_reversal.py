#!/usr/bin/env python3
"""Lightweight tests without pytest."""
from paper_reversal_sim import run_scenarios


def test_reversal_scenarios():
    results, failed = run_scenarios()
    for r in results:
        mark = "PASS" if r.get("ok") else "FAIL"
        print(f"{mark} {r['name']}")
    assert failed == 0, f"{failed} scenario(s) failed"


def main():
    test_reversal_scenarios()


if __name__ == "__main__":
    main()
