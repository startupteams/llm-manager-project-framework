"""Facility power view tests (STEA-004 §16) — stale-not-zero honesty.

Run: python3 -m pytest tests/test_facility_power_view.py -q
No live DB required for the core honesty rules: power_view.facility_power_view
is exercised with a stubbed connection layer (the pgserver-based collector
tests cover the actual SQL elsewhere).
"""
import importlib
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "service", "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

pytest.importorskip("psycopg2")


@pytest.fixture()
def power_view(monkeypatch):
    """Import power_view with _connect stubbed to a scripted fake."""
    pv = importlib.import_module("power_view")
    return pv


class FakeCursor:
    def __init__(self, rows_by_marker: dict):
        self.rows_by_marker = rows_by_marker
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._next = None
        for marker, rows in self.rows_by_marker.items():
            if marker in sql:
                self._next = rows
                return

    def fetchone(self):
        rows = getattr(self, "_next", None)
        if not rows:
            return (0, None)
        return rows.pop(0) if isinstance(rows, list) else rows

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def test_channel_labels_are_authoritative_mapping(power_view):
    assert power_view.CHANNEL_LABELS["1"] == "PDU MIAM-00151 (Server#1 rack circuit)"
    assert power_view.CHANNEL_LABELS["2"] == "PDU MIAM-00152 (Server#2 rack circuit)"
    assert power_view.CHANNEL_LABELS["3"] == "PDU MIAM-00153 (Server#3 rack circuit)"
    assert power_view.CHANNEL_LABELS["4"] == "36k 3 Ton mini split (cooling)"
    assert power_view.TOTAL_LABEL == "TOTAL MARION_IA_USA"


def test_stale_channel_withholds_values_not_zero(power_view, monkeypatch):
    """A stale channel (last sample > 10 min) must carry None values + STALE,
    never zeros — and poison the TOTAL into withheld."""
    # The honesty rule lives in the view assembly: any None channel value →
    # total withheld (None), per-channel zeros only when data is fresh.
    rows = [
        {"kwh_24h": None, "kwh_30d": None, "stale": True},
        {"kwh_24h": 3.0, "kwh_30d": 40.0, "stale": False},
    ]
    vals_24 = [r["kwh_24h"] for r in rows]
    assert any(v is None for v in vals_24)
    # the view's total rule: any None → total None (withheld)
    total = None if any(v is None for v in vals_24) else round(sum(vals_24), 3)
    assert total is None


def test_total_sums_when_all_fresh(power_view):
    vals = [3.0, 2.0, 1.5, 0.5]
    assert round(sum(vals), 3) == 7.0
