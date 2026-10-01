"""Server Manager API — facility power (STEA-004 plan §17 ACMS ingest).

Exposes the SAME view LLM Manager's /api/facility/power renders, over the
machine API with scoped service-token auth (usage:read), so ACMS can ingest
facility energy/cost WITHOUT duplicating Emporia credentials or the llmmanager
DB password into ACMS (plan §17: 'Create a supported internal integration from
LLM Manager power API to ACMS').

Semantics are copied from service/app/power_view.py (channel windows, per-
channel staleness, TOTAL withheld when any channel is stale — never zero-
filled). Auth mapping and rate constants match the app (Rate 400 components
when present, else built-in plan v0.2.1).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from server_manager.common.auth.service_tokens import require_identity
from server_manager.common.db.session import get_session_factory

router = APIRouter(prefix="/api/v1/facility", tags=["server-manager-facility"])

STALE_SAMPLE_SECONDS = 600  # matches app power_view.py

# Authoritative mapping (LLM Manager power_view.py, 2026-10-01)
CHANNEL_LABELS = {
    "1": "PDU MIAM-00151 (Server#1 rack circuit)",
    "2": "PDU MIAM-00152 (Server#2 rack circuit)",
    "3": "PDU MIAM-00153 (Server#3 rack circuit)",
    "4": "36k 3 Ton mini split (cooling)",
}
TOTAL_LABEL = "TOTAL MARION_IA_USA"

# Built-in plan v0.2.1 constants (mirror of app/main.py; electricity_rates
# components override when present)
RATE_SUMMER_BASE = 0.13234
RATE_WINTER_BASE = 0.10257
RATE_FEES_RIDERS = 0.04540
SUMMER_MONTHS = {6, 7, 8}


def _effective_rate(session, month: int) -> tuple[float, str]:
    season = "summer" if month in SUMMER_MONTHS else "winter"
    base = RATE_SUMMER_BASE if season == "summer" else RATE_WINTER_BASE
    fees = RATE_FEES_RIDERS
    source = "built-in plan v0.2.1 constants"
    try:
        rows = session.execute(text(
            "SELECT component, value_per_kwh FROM electricity_rates "
            "WHERE season = :season AND effective_from <= CURRENT_DATE "
            "AND (effective_to IS NULL OR effective_to >= CURRENT_DATE) "
            "ORDER BY rate_id DESC"), {"season": season}).mappings().all()
        vals = {r["component"]: float(r["value_per_kwh"]) for r in rows}
        if "fees_riders" in vals:
            fees = vals["fees_riders"]
        if "base" in vals:
            base = vals["base"]
        if "base" in vals or "fees_riders" in vals:
            source = "electricity_rates (versioned components)"
    except Exception:  # noqa: BLE001 — rate table optional
        pass
    return round(base + fees, 5), source


@router.get("/power")
def facility_power(request: Request):
    """Per-channel + TOTAL MARION_IA_USA energy/cost, collector health,
    honest staleness. Service-token scope: usage:read."""
    ident = require_identity(request, "usage:read")
    Session = get_session_factory("llm")
    out: dict = {
        "error": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested_by": ident.service,
        "channels": [],
        "totals": {},
        "collector": {},
        "rate": {},
    }
    with Session() as session:
        exists = session.execute(text(
            "SELECT to_regclass('facility_power_samples') IS NOT NULL")).scalar()
        if not exists:
            out["error"] = "Emporia collector not deployed (facility_power_samples missing)"
            return out

        rate, rate_src = _effective_rate(session, datetime.now(timezone.utc).month)
        out["rate"] = {"effective_per_kwh": rate,
                       "display_cents": round(rate * 100, 2),
                       "season": "summer" if datetime.now(timezone.utc).month in SUMMER_MONTHS else "winter",
                       "source": rate_src}

        n_1h = session.execute(text(
            "SELECT count(*), max(ts) FROM facility_power_samples "
            "WHERE ts >= now() - interval '1 hour'")).one()
        n_total = session.execute(text(
            "SELECT count(*) FROM facility_power_samples")).scalar()
        last_ts, last_age_s = n_1h[1], None
        if last_ts is not None:
            last_age_s = float(session.execute(text(
                "SELECT EXTRACT(epoch FROM (now() - :ts))"), {"ts": last_ts}).scalar())
        stale = last_ts is None or (last_age_s is not None and last_age_s > STALE_SAMPLE_SECONDS)
        out["collector"] = {
            "samples_1h": int(n_1h[0] or 0),
            "samples_total": int(n_total or 0),
            "last_sample_ts": last_ts.isoformat() if last_ts else None,
            "last_sample_age_seconds": round(last_age_s) if last_age_s is not None else None,
            "healthy": bool(n_1h[0]),
            "stale": bool(stale),
            "stale_policy": "stale data shows STALE + timestamp — NEVER rendered as 0",
        }

        for cnum in ("1", "2", "3", "4"):
            label = CHANNEL_LABELS[cnum]
            avg_w = session.execute(text(
                "SELECT AVG(s.watts) FROM facility_power_samples s "
                "WHERE s.channel_num = :c AND s.ts >= now() - interval '1 hour'"), {"c": cnum}).scalar()
            kwh_24h = session.execute(text(
                "SELECT SUM(s.kwh_interval) FROM facility_power_samples s "
                "WHERE s.channel_num = :c AND s.ts >= now() - interval '24 hours'"), {"c": cnum}).scalar()
            kwh_30d = session.execute(text(
                "SELECT SUM(s.kwh_interval) FROM facility_power_samples s "
                "WHERE s.channel_num = :c AND s.ts >= now() - interval '30 days'"), {"c": cnum}).scalar()
            ch_last = session.execute(text(
                "SELECT max(ts) FROM facility_power_samples s WHERE s.channel_num = :c"), {"c": cnum}).scalar()
            ch_age = None
            if ch_last is not None:
                ch_age = float(session.execute(text(
                    "SELECT EXTRACT(epoch FROM (now() - :ts))"), {"ts": ch_last}).scalar())
            ch_stale = ch_last is None or (ch_age is not None and ch_age > STALE_SAMPLE_SECONDS)
            out["channels"].append({
                "channel_num": cnum,
                "label": label,
                "avg_watts_1h": round(float(avg_w), 1) if avg_w is not None and not ch_stale else None,
                "kwh_24h": round(float(kwh_24h), 3) if kwh_24h is not None and not ch_stale else None,
                "cost_usd_24h": round(float(kwh_24h) * rate, 4) if kwh_24h is not None and not ch_stale else None,
                "kwh_30d": round(float(kwh_30d), 3) if kwh_30d is not None and not ch_stale else None,
                "cost_usd_30d": round(float(kwh_30d) * rate, 4) if kwh_30d is not None and not ch_stale else None,
                "last_sample_ts": ch_last.isoformat() if ch_last else None,
                "last_sample_age_seconds": round(ch_age) if ch_age is not None else None,
                "stale": bool(ch_stale),
            })

        def _sum_ch(field: str) -> float | None:
            vals = [c.get(field) for c in out["channels"]]
            if any(v is None for v in vals):
                return None  # any stale channel poisons the total honestly
            return round(sum(float(v) for v in vals), 3)

        t24, t30 = _sum_ch("kwh_24h"), _sum_ch("kwh_30d")
        out["totals"] = {
            "label": TOTAL_LABEL,
            "definition": "PDU MIAM-00151 + PDU MIAM-00152 + PDU MIAM-00153 + mini split",
            "kwh_24h": t24,
            "cost_usd_24h": round(t24 * rate, 4) if t24 is not None else None,
            "kwh_30d": t30,
            "cost_usd_30d": round(t30 * rate, 4) if t30 is not None else None,
            "incomplete_reason": ("one or more channels STALE — total withheld (never zero-filled)"
                                  if t24 is None else None),
        }
    return out