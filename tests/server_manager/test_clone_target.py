"""Clone-target regression test (live-found 2026-10-02, plan §26 W4 probe).

The PVE clone API call MUST set target = the PLACED node (spec.node). The old
code pinned target=template_node, which left the cloned VM on the template
node while the lock-wait polled the placed node — the first-ever sandbox
provision exposed it (240s lock-wait timeout + orphan VM on miam00111).
"""
from __future__ import annotations

from server_manager.agent_runtime_manager.providers.proxmox_vm import (
    ProxmoxVMProvider,
    VMSpec,
)


class RecordingProvider(ProxmoxVMProvider):
    """ProxmoxVMProvider with a _call that records instead of HTTPing."""

    def __init__(self):  # skip the real init (settings/creds)
        self.calls: list[tuple[str, str, dict | None]] = []

    def _call(self, method, path, body=None, timeout=30):
        self.calls.append((method, path, body))
        return {"data": None}


def test_clone_target_is_placed_node():
    p = RecordingProvider()
    spec = VMSpec(name="sbx-test", node="miam-00100", template_vmid=121,
                  template_node="miam00111")
    p.clone_template(spec, 200)

    method, path, body = p.calls[0]
    assert method == "POST"
    assert path == "/nodes/miam00111/qemu/121/clone"  # SOURCE = template node
    assert body["target"] == "miam-00100"             # DEST = PLACED node (the fix)
    assert body["newid"] == 200
    assert body["full"] == 1


def test_sandbox_placement_pins_to_template_node(arm_session):
    # plan §26 live-found: node-local template storage -> sandboxes pin to the
    # template node (cross-node clone would 500 when testthin inactive).
    from server_manager.agent_runtime_manager.services.placement import select_node

    class _FakeProvider:
        def list_nodes(self):
            return [("miam-00100", "online"), ("miam00111", "online")]

    decision = select_node(arm_session, _FakeProvider(),
                           runtime_class="sandbox", template_node="miam00111")
    assert not decision.rejected
    assert decision.node == "miam00111"
    assert "sandbox_pinned_to_template_node" in decision.reasons


def test_dkms_service_placement_pins_to_template_node(arm_session):
    # DKMS live-found 2026-10-03: dkms_service VMs clone from the node-local
    # golden template (testthin on miam00111) — same constraint as sandboxes.
    from server_manager.agent_runtime_manager.services.placement import select_node

    class _FakeProvider:
        def list_nodes(self):
            return [("miam-00100", "online"), ("miam00111", "online")]

    decision = select_node(arm_session, _FakeProvider(),
                           runtime_class="dkms_service", template_node="miam00111")
    assert not decision.rejected
    assert decision.node == "miam00111"
    assert "dkms_service_pinned_to_template_node" in decision.reasons
