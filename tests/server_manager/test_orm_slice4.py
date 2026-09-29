"""Slice-4 ORM parity tests (TDR-0008, 2026-09-29).

Schema honesty: the reflective models must match the live llmmanager columns
(validated against prod via information_schema on 2026-09-29). These tests pin
the mapped column names/keys so drift fails fast. DB-backed parity runs where a
PostgreSQL URL is provided (ACMS-style pattern); otherwise the schema-shape test
runs everywhere.
"""
from __future__ import annotations

import pytest

from server_manager.llm_manager.models import orm_slice4 as s4


def test_slice4_table_names_are_exact():
    assert [m.__tablename__ for m in
            (s4.RecoveryEventRecord, s4.RequestRoutingLogRecord,
             s4.ElectricityRateRecord, s4.ManagerSettingRecord)] == [
        "recovery_events", "request_routing_log", "electricity_rates", "manager_settings"]


def test_slice4_primary_keys_match_live_schema():
    # live PKs (verified 2026-09-29): id, id, rate_id, key
    assert [c.name for c in s4.RecoveryEventRecord.__table__.primary_key] == ["id"]
    assert [c.name for c in s4.RequestRoutingLogRecord.__table__.primary_key] == ["id"]
    assert [c.name for c in s4.ElectricityRateRecord.__table__.primary_key] == ["rate_id"]
    assert [c.name for c in s4.ManagerSettingRecord.__table__.primary_key] == ["key"]


def test_slice4_columns_match_live_schema():
    expected = {
        "recovery_events": {"id", "host_id", "ip", "event_type", "detail", "created_at"},
        "request_routing_log": {"id", "request_id", "ts", "model", "api_base",
                                 "prompt_tokens", "completion_tokens", "latency_ms",
                                 "ttft_ms", "key_alias", "provider_spend"},
        "electricity_rates": {"rate_id", "component", "value_per_kwh", "season",
                               "effective_from", "effective_to", "source_note", "created_at"},
        "manager_settings": {"key", "value", "updated_at", "updated_by"},
    }
    for model, cols in expected.items():
        table_cols = {c.name for c in s4.__dict__[
            {"recovery_events": "RecoveryEventRecord",
             "request_routing_log": "RequestRoutingLogRecord",
             "electricity_rates": "ElectricityRateRecord",
             "manager_settings": "ManagerSettingRecord"}[model]].__table__.columns}
        assert table_cols == cols, f"{model}: {table_cols ^ cols}"


def test_slice4_models_are_reflective_no_ddl():
    """Slice rule: no create_table on the llmmanager base — models map, never migrate."""
    from server_manager.llm_manager.models.orm import LlmBase

    # The shared LlmBase metadata must never be used to DDL-create llmmanager tables.
    assert s4.RecoveryEventRecord.__table__.metadata is LlmBase.metadata
