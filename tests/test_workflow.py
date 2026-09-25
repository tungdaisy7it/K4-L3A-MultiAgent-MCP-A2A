from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import ToolCallError
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "order-synthetic-0001"
SELLER_ID = "seller-synthetic"
ITEM_ID = "item-synthetic"
PLATFORM = [{"party_type": "platform", "party_id": None}]
CUSTOMER = [{"party_type": "customer", "party_id": None}]
POLICY = {
    "currency": "BRL",
    "policy_version": "TEST_POLICY",
    "rules": {
        "canceled_order_paid": {
            "case_status": "action_required", "recommended_action": "issue_refund",
            "refund_brl": 79.0, "responsible_parties": PLATFORM,
        },
        "late_delivery_seller": {
            "case_status": "action_required", "recommended_action": "refund_freight",
            "refund_brl": 18.0,
            "responsible_parties": [{"party_type": "seller", "party_id": "seller-template"}],
        },
        "valid_split_payment": {
            "case_status": "no_action", "recommended_action": "document_no_action",
            "refund_brl": 0.0, "responsible_parties": CUSTOMER,
        },
        "duplicate_charge": {
            "case_status": "action_required", "recommended_action": "refund_duplicate_charge",
            "refund_brl": 64.0,
            "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
        },
        "unsupported_claim": {
            "case_status": "no_action", "recommended_action": "document_no_action",
            "refund_brl": 0.0, "responsible_parties": CUSTOMER,
        },
    },
}


def _ts(day: str, hour: int = 9) -> str:
    return f"2018-{day}T{hour:02d}:00:00-03:00"


class FakeGateway:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[tuple[str, str]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id))
        if tool_name not in self.data:
            raise ToolCallError(f"MCP tool {tool_name} failed: not found")
        domain = {
            "get_order": "order", "get_order_items": "item", "get_payment_timeline": "payment",
            "get_refund_timeline": "refund", "get_shipment_summary": "shipment",
            "get_sellers": "seller", "get_policy": "policy",
        }[tool_name]
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name.replace('_', '')}{case_id.replace('_', '')}0000",
            "result_hash": "sha256:" + "0" * 64,
            "domain": domain,
            "data": self.data[tool_name],
        }


def _evidence(*, status: str = "delivered", delivered: str | None = "03-10",
              carrier: str = "03-03", captures: list[tuple[str, str]] | None = None,
              late_actor: str | None = None) -> dict[str, Any]:
    captures = captures or [("03-01", "89.00")]
    items = [
        {"order_id": ORDER_ID, "order_item_id": ITEM_ID, "product_id": "p", "seller_id": SELLER_ID,
         "shipping_limit_date": _ts("03-04"), "price": "79.00", "freight_value": "10.00"},
        # Contaminating row far outside the case window must be ignored.
        {"order_id": ORDER_ID, "order_item_id": ITEM_ID, "product_id": "p", "seller_id": SELLER_ID,
         "shipping_limit_date": _ts("07-01"), "price": "79.00", "freight_value": "18.00"},
    ]
    events = [{"order_id": ORDER_ID, "event_at": _ts(day, 10 + index), "event_type": "captured",
               "amount_brl": amount, "status": "confirmed"}
              for index, (day, amount) in enumerate(captures)]
    events.append({"order_id": ORDER_ID, "event_at": _ts("07-02"), "event_type": "captured",
                   "amount_brl": "18.00", "status": "confirmed"})
    shipment_events = []
    if late_actor:
        shipment_events.append({"order_id": ORDER_ID, "event_at": _ts(delivered or "03-10"),
                                "event_type": "delivered_late", "actor": late_actor,
                                "status": "confirmed"})
    return {
        "get_order": {
            "order_id": ORDER_ID, "customer_id": "c", "order_status": status,
            "order_purchase_timestamp": _ts("03-01"), "order_approved_at": _ts("03-01", 10),
            "order_delivered_carrier_date": _ts(carrier),
            "order_delivered_customer_date": _ts(delivered) if delivered else None,
            "order_estimated_delivery_date": _ts("03-09"),
        },
        "get_order_items": items,
        "get_payment_timeline": {"order_id": ORDER_ID, "payments": [], "events": events},
        "get_shipment_summary": {
            "order_id": ORDER_ID, "order_status": status, "delivered_carrier_at": _ts(carrier),
            "delivered_customer_at": _ts(delivered) if delivered else None,
            "estimated_delivery_at": _ts("03-09"), "shipping_limits": [], "events": shipment_events,
        },
        "get_sellers": [{"seller_id": SELLER_ID, "seller_city": "x", "seller_state": "SP",
                         "seller_zip_code_prefix": "01001"}],
        "get_policy": POLICY,
    }


def _solve(tmp_path: Path, data: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {
        "case_id": "TEST_CASE_001", "opened_at": _ts("03-20"), "policy_version": "TEST_POLICY",
        "customer_request": {
            "claimed_order_id": ORDER_ID,
            "claims": [{"claim_id": "c-a", "topic": "late_delivery_seller"},
                       {"claim_id": "c-b", "topic": "requested_full_refund"}],
        },
    }
    output = asyncio.run(solve_case(case, FakeGateway(data), trace))
    contracts.validate_output(output, "output")
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    return output, events


def test_late_delivery_by_seller_binds_order_seller(tmp_path: Path) -> None:
    output, events = _solve(tmp_path, _evidence(carrier="03-06", late_actor="seller"))
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["financial_resolution"]["recommended_refund_brl"] == 18.0
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": SELLER_ID}
    ]
    assert output["affected_entities"]["seller_ids"] == [SELLER_ID]
    consumed = {ref for event in events if event["event_type"] == "tool_result_consumed"
                for ref in event["evidence_refs"]}
    assert set(output["evidence_refs"]) <= consumed


def test_on_time_delivery_is_unsupported(tmp_path: Path) -> None:
    output, _ = _solve(tmp_path, _evidence(delivered="03-08"))
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"] == {
        "currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []
    }


@pytest.mark.parametrize(
    ("captures", "expected"),
    [
        ([("03-01", "44.50"), ("03-01", "44.50")], "valid_split_payment"),
        ([("03-01", "64.00"), ("03-01", "64.00")], "duplicate_charge"),
    ],
)
def test_payment_patterns_compare_with_order_total(
    tmp_path: Path, captures: list[tuple[str, str]], expected: str
) -> None:
    output, _ = _solve(tmp_path, _evidence(delivered="03-08", captures=captures))
    assert output["assessment"]["primary_issue"] == expected


def test_canceled_order_takes_priority(tmp_path: Path) -> None:
    output, events = _solve(tmp_path, _evidence(status="canceled", delivered=None))
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["resolution_actions"] == ["issue_refund"]
    assert output["financial_resolution"]["refund_lines"][0]["entity_id"] == ORDER_ID
    required = {"task_assigned", "handoff", "policy_decided", "verification_completed"}
    assert required <= {event["event_type"] for event in events}


def test_missing_order_is_insufficient_evidence(tmp_path: Path) -> None:
    data = _evidence()
    del data["get_order"]
    output, _ = _solve(tmp_path, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["affected_entities"]["order_ids"] == []
