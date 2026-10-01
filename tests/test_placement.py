"""STEA-004 §5: ARM placement-policy tests (code, not convention)."""

from __future__ import annotations

import pytest

from server_manager.agent_runtime_manager.services.placement import (
    EXCLUDED_NODES,
    GENERAL_PREFERRED_NODE,
    INFERENCE_NODES,
    select_node,
)


class FakeProvider:
    def __init__(self, nodes: list[tuple[str, str]]):
        self._nodes = nodes

    def list_nodes(self):
        return list(self._nodes)


class FakeSession:
    """Minimal session stand-in: returns canned worker-count rows."""

    def __init__(self, worker_rows: list[tuple[str, str]]):
        self._rows = worker_rows

    def execute(self, _stmt):
        return self

    def all(self):
        return self._rows


ONLINE = [
    ("miam00111", "online"), ("miam00112", "online"),
    ("miam00143", "online"), ("miam00144", "online"),
    ("miam-00100", "online"), ("miam-00133", "online"),
    ("miam-00147", "online"), ("miam-00135", "online"),
]


def test_general_worker_prefers_miam00100():
    dec = select_node(FakeSession([]), FakeProvider(ONLINE), runtime_class="software_development_worker")
    assert not dec.rejected
    assert dec.node == GENERAL_PREFERRED_NODE
    assert "general_preferred" in dec.reasons


def test_excluded_nodes_never_selected():
    assert "miam-00133" in EXCLUDED_NODES
    assert "miam-00147" in EXCLUDED_NODES
    # even if MIAM00100 is offline, exclusions hold
    nodes = [("miam-00133", "online"), ("miam-00147", "online"), ("miam-00135", "online")]
    dec = select_node(FakeSession([]), FakeProvider(nodes), runtime_class="software_development_worker")
    assert not dec.rejected
    assert dec.node == "miam-00135"


def test_inference_worker_capped_one_per_node():
    # one inference-class worker already on miam00111 → must NOT place a second there
    rows = [("miam00111", "inference")]
    dec = select_node(FakeSession(rows), FakeProvider(ONLINE), runtime_class="inference")
    assert not dec.rejected
    assert dec.node != "miam00111"
    # every inference node at cap + no general node → still rejects ONLY if no
    # generic node exists either; here miam-00135 is a valid generic fallback
    rows_full = [(n, "inference") for n in INFERENCE_NODES]
    no_generic = [(n, "online") for n in ONLINE
                  if n[0] != "miam-00100" and n[0] != "miam-00135"
                  and n[0] not in EXCLUDED_NODES and n[0] not in INFERENCE_NODES]
    dec3 = select_node(FakeSession(rows_full), FakeProvider(no_generic), runtime_class="inference")
    assert dec3.rejected
    assert dec3.rejection_reason == "no_eligible_node"


def test_general_worker_on_inference_node_gets_penalty():
    # MIAM00100 offline → fall through; inference nodes allowed but penalized
    nodes = [(n, "online") for n, _ in ONLINE if n != "miam-00100"]
    dec = select_node(FakeSession([]), FakeProvider(nodes), runtime_class="software_development_worker")
    assert not dec.rejected
    assert dec.node == "miam-00135" or dec.node in INFERENCE_NODES
    # the exclusion guarantee is structural; re-assert on this mixed pool too
    assert dec.node not in EXCLUDED_NODES
    if dec.node in INFERENCE_NODES:
        assert "inference_node_penalty" in dec.reasons


def test_offline_node_rejected():
    nodes = [("miam-00100", "offline"), ("miam00112", "online")]
    dec = select_node(FakeSession([]), FakeProvider(nodes), runtime_class="inference")
    assert not dec.rejected
    assert dec.node == "miam00112"


def test_no_online_nodes_fails_closed():
    dec = select_node(FakeSession([]), FakeProvider([]), runtime_class="software_development_worker")
    assert dec.rejected
    assert dec.rejection_reason == "no_eligible_node"