"""Phase H: clone identity/network hygiene tests (fail-closed)."""

from __future__ import annotations

import pytest

from server_manager.agent_runtime_manager.services.clone_hygiene import (
    HygieneError,
    verify_clone_identity,
)


def test_all_clean_passes(monkeypatch):
    monkeypatch.setattr(
        "server_manager.agent_runtime_manager.services.clone_hygiene._guest_netplan_static_ips",
        lambda node, vmid: ["10.0.20.203"],
    )
    rep = verify_clone_identity(
        vmid=124, hostname="acms-worker-001", mac="BC:24:11:AA:BB:CC",
        ip="10.0.20.203", expected_agent_id="agent-x", guest_agent_id="agent-x",
        other_runtime_ips={"10.0.20.204"},
    )
    assert rep.ok


def test_inherited_netplan_fails_closed(monkeypatch):
    """THE VM108/VM124 lesson: template netplan still claiming another IP."""
    monkeypatch.setattr(
        "server_manager.agent_runtime_manager.services.clone_hygiene._guest_netplan_static_ips",
        lambda node, vmid: ["10.0.20.203"],  # template-carried static
    )
    with pytest.raises(HygieneError, match="netplan_reidentified"):
        verify_clone_identity(
            vmid=125, hostname="worker-2", mac="BC:24:11:AA:BB:CD",
            ip="10.0.20.205", expected_agent_id="agent-y", guest_agent_id="agent-y",
            other_runtime_ips=set(),
        )


def test_mac_collision_fails(monkeypatch):
    monkeypatch.setattr(
        "server_manager.agent_runtime_manager.services.clone_hygiene._guest_netplan_static_ips",
        lambda node, vmid: [],
    )
    with pytest.raises(HygieneError, match="mac_valid"):
        verify_clone_identity(
            vmid=126, hostname="worker-3", mac="",
            ip="10.0.20.206", expected_agent_id="agent-z", guest_agent_id="agent-z",
            other_runtime_ips=set(),
        )


def test_template_hostname_fails():
    with pytest.raises(HygieneError, match="hostname_reidentified"):
        verify_clone_identity(
            vmid=127, hostname="template-clone", mac="BC:24:11:11:11:11",
            ip=None, expected_agent_id="a", guest_agent_id="a",
            other_runtime_ips=set(),
        )


def test_guest_identity_mismatch_fails():
    with pytest.raises(HygieneError, match="guest_identity_matches"):
        verify_clone_identity(
            vmid=128, hostname="worker-5", mac="BC:24:11:22:22:22",
            ip=None, expected_agent_id="expected-agent",
            guest_agent_id="different-agent",
            other_runtime_ips=set(),
        )


def test_ip_owned_by_other_runtime_fails():
    with pytest.raises(HygieneError, match="ip_not_owned_by_other_runtime"):
        verify_clone_identity(
            vmid=129, hostname="worker-6", mac="BC:24:11:33:33:33",
            ip="10.0.20.207", expected_agent_id="a", guest_agent_id="a",
            other_runtime_ips={"10.0.20.207"},
        )
