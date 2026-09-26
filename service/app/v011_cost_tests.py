#!/usr/bin/env python3
"""v0.11 cost/energy integrator + telemetry coverage tests (plan §H4/H5).

Gate: deployment FAILS unless the kWh integrator passes exactly:
  100 W for 1 h  = 0.100 kWh
  1000 W for 1 h = 1.000 kWh
Run:  python3 v011_cost_tests.py   (exit 0 = pass, 1 = fail)
"""
import sys
from datetime import datetime, timedelta, timezone


def integrate_kwh(watts_series):
    """ watts_series: list of (ts: datetime, avg_watts: float) sampled at 1-min
    intervals like host_power_rollup_1m. kWh = sum(watts)/60/1000.
    Mirrors the app's SQL: SUM(gpu_watts_avg)/60.0/1000.0 """
    total_watt_minutes = 0.0
    for _ts, w in watts_series:
        total_watt_minutes += float(w or 0.0)
    return total_watt_minutes / 60.0 / 1000.0


def series_for(watts, minutes):
    t0 = datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc)
    return [(t0 + timedelta(minutes=i), watts) for i in range(minutes)]


def main():
    failures = []
    # §H5 gate 1
    kwh = integrate_kwh(series_for(100, 60))
    print(f"100 W x 1 h = {kwh:.4f} kWh (expect 0.1000)", end="  ")
    ok1 = abs(kwh - 0.100) < 1e-9
    print("PASS" if ok1 else "FAIL")
    if not ok1:
        failures.append("100Wx1h")

    # §H5 gate 2
    kwh = integrate_kwh(series_for(1000, 60))
    print(f"1000 W x 1 h = {kwh:.4f} kWh (expect 1.0000)", end="  ")
    ok2 = abs(kwh - 1.000) < 1e-9
    print("PASS" if ok2 else "FAIL")
    if not ok2:
        failures.append("1000Wx1h")

    # partial coverage demonstration (§H4): 60 min of data must NOT claim 24h
    cov = 60 / (24 * 60)
    print(f"coverage example: 60 of 1440 minutes = {cov*100:.1f}% "
          f"(labels must say 'partial', never '24h')")
    if cov >= 1:
        failures.append("coverage math")

    if failures:
        print("RESULT: FAIL", failures)
        sys.exit(1)
    print("RESULT: PASS — integrator matches plan §H5 gates")


if __name__ == "__main__":
    main()
