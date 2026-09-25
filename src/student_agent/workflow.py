"""L3A multi-agent workflow: coordinator, specialists (order, payment, shipment, policy), verifier.

Every agent is a small deterministic component. Agents only exchange ``Handoff`` envelopes
through the coordinator, only call the MCP tools they own, and every conclusion is derived
from MCP evidence that is scoped to the active ``case_id``. The trace records observable
events (assignments, consumed tool results, handoffs, decisions) and never reasoning text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway, ToolCallError
from .trace import TraceWriter

COORDINATOR = "coordinator"
ORDER_AGENT = "order-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier"

# Least-privilege tool ownership: an agent can only call the tools listed for it.
TOOL_OWNERSHIP: dict[str, frozenset[str]] = {
    ORDER_AGENT: frozenset({"get_order", "get_order_items", "get_sellers"}),
    PAYMENT_AGENT: frozenset({"get_payment_timeline", "get_refund_timeline"}),
    SHIPMENT_AGENT: frozenset({"get_shipment_summary"}),
    POLICY_AGENT: frozenset({"get_policy"}),
    VERIFIER: frozenset(),
    COORDINATOR: frozenset(),
}

# Evidence groups cited for each primary issue. Only groups that support the conclusion are
# cited; product and customer-history evidence is never needed for these decisions.
CITED_GROUPS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("order", "payment", "policy"),
    "unavailable_order_paid": ("order", "item", "seller", "payment", "policy"),
    "late_delivery_seller": ("order", "item", "seller", "shipment", "policy"),
    "late_delivery_logistics": ("order", "shipment", "policy"),
    "valid_split_payment": ("order", "item", "payment", "policy"),
    "payment_mismatch": ("order", "payment", "policy"),
    "duplicate_charge": ("order", "item", "payment", "policy"),
    "refund_pending": ("order", "payment", "refund", "policy"),
    "refund_failed": ("order", "payment", "refund", "policy"),
    "unsupported_claim": ("order", "payment", "shipment", "policy"),
    "insufficient_evidence": ("order", "policy"),
}

SELLER_RESPONSIBLE = frozenset({"unavailable_order_paid", "late_delivery_seller"})
FULL_ORDER_ISSUES = frozenset({"canceled_order_paid", "unavailable_order_paid"})
# Issues whose policy refund returns the whole amount the customer paid for the order.
FULL_REFUND_ISSUES = FULL_ORDER_ISSUES | {"refund_failed"}
FALLBACK_ACTION = "request_manual_review"
CENT = Decimal("0.01")
# Charges captured this close to order approval belong to the checkout itself.
ANCHOR_WINDOW = timedelta(hours=24)


class WorkflowError(RuntimeError):
    """Raised when a specialist result violates the workflow contract."""


@dataclass
class Evidence:
    tool_name: str
    domain: str
    ref: str
    data: Any


@dataclass
class Handoff:
    """A2A envelope exchanged between agents; always correlated by ``case_id``."""

    case_id: str
    sender: str
    recipient: str
    task: str
    findings: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _near(value: Any, anchor: Any, tolerance: timedelta) -> bool:
    moment, reference = _ts(value), _ts(anchor)
    return bool(moment and reference and abs(moment - reference) <= tolerance)


def _money(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(CENT)
    except (ArithmeticError, ValueError):
        return None


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


class CaseWindow:
    """Records are relevant only between the order purchase and the case opening time."""

    def __init__(self, start: datetime | None, end: datetime | None) -> None:
        self.start = start
        self.end = end

    def contains(self, value: Any) -> bool:
        moment = _ts(value)
        if moment is None or self.end is None:
            return False
        if self.start is not None and moment < self.start:
            return False
        return moment <= self.end


class Agent:
    name = "agent"

    def __init__(self, case_id: str, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace

    async def fetch(self, tool_name: str, **arguments: str) -> Evidence | None:
        """Call an owned MCP tool; ``None`` means the gateway reported no record."""
        if tool_name not in TOOL_OWNERSHIP[self.name]:
            raise WorkflowError(f"{self.name} is not allowed to call {tool_name}")
        try:
            response = await self.gateway.call(tool_name, case_id=self.case_id, **arguments)
        except ToolCallError:
            return None
        evidence = Evidence(tool_name, response["domain"], response["evidence_ref"],
                            response["data"])
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=self.name,
            tool_name=tool_name,
            evidence_refs=[evidence.ref],
            attributes={"domain": evidence.domain},
        )
        return evidence

    def handoff(self, task: str, findings: dict[str, Any], evidence: dict[str, Evidence],
                decision_code: str) -> Handoff:
        message = Handoff(self.case_id, self.name, COORDINATOR, task, findings, evidence)
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=self.name,
            target=COORDINATOR,
            decision_code=decision_code,
            evidence_refs=[item.ref for item in evidence.values()] or None,
            attributes={"task": task},
        )
        return message


class OrderAgent(Agent):
    name = ORDER_AGENT

    async def run(self, order_id: str, window_end: datetime | None) -> Handoff:
        evidence: dict[str, Evidence] = {}
        order_ev = await self.fetch("get_order", order_id=order_id)
        order = order_ev.data if order_ev and isinstance(order_ev.data, dict) else None
        if order is None or order.get("order_id") != order_id:
            return self.handoff("order_context", {"found": False}, evidence, "ORDER_NOT_FOUND")
        evidence["order"] = order_ev
        window = CaseWindow(_ts(order.get("order_purchase_timestamp")), window_end)

        items_ev = await self.fetch("get_order_items", order_id=order_id)
        rows = [row for row in _as_list(items_ev.data if items_ev else None)
                if row.get("order_id") == order_id]
        if items_ev:
            evidence["item"] = items_ev
        in_window = [row for row in rows if window.contains(row.get("shipping_limit_date"))]
        # The authoritative item row is the first shipping commitment inside the case window.
        in_window.sort(key=lambda row: _ts(row["shipping_limit_date"]))
        primary = in_window[0] if in_window else None
        findings = {
            "found": True,
            "order_id": order_id,
            "status": order.get("order_status"),
            "purchase_at": order.get("order_purchase_timestamp"),
            "approved_at": order.get("order_approved_at"),
            "carrier_at": order.get("order_delivered_carrier_date"),
            "customer_at": order.get("order_delivered_customer_date"),
            "estimated_at": order.get("order_estimated_delivery_date"),
            "primary_item": primary,
            "item_ids": sorted({str(row["order_item_id"]) for row in in_window
                                if row.get("order_item_id")}),
            "seller_ids": sorted({str(row["seller_id"]) for row in in_window
                                  if row.get("seller_id")}),
            "excluded_item_rows": len(rows) - len(in_window),
            "conflicting_item_fields": sorted(
                name for name in ("price", "freight_value", "shipping_limit_date")
                if len({str(row.get(name)) for row in rows}) > 1
            ),
        }
        code = "ORDER_CONTEXT_READY" if primary else "ORDER_ITEMS_MISSING"
        return self.handoff("order_context", findings, evidence, code)

    async def run_seller_lookup(self, order_id: str, seller_ids: list[str]) -> Handoff:
        evidence: dict[str, Evidence] = {}
        sellers_ev = await self.fetch("get_sellers", order_id=order_id)
        known = {str(row.get("seller_id")) for row in _as_list(sellers_ev.data if sellers_ev
                                                                  else None)}
        confirmed = [seller for seller in seller_ids if seller in known]
        if sellers_ev and confirmed:
            evidence["seller"] = sellers_ev
        code = "SELLER_CONFIRMED" if confirmed else "SELLER_NOT_CONFIRMED"
        return self.handoff("seller_lookup", {"seller_ids": confirmed}, evidence, code)


class PaymentAgent(Agent):
    name = PAYMENT_AGENT

    async def run(self, order_id: str, window: CaseWindow, approved_at: str | None) -> Handoff:
        evidence: dict[str, Evidence] = {}
        payment_ev = await self.fetch("get_payment_timeline", order_id=order_id)
        data = payment_ev.data if payment_ev and isinstance(payment_ev.data, dict) else {}
        if payment_ev:
            evidence["payment"] = payment_ev
        events = [event for event in _as_list(data.get("events"))
                  if event.get("order_id") in (None, order_id)]
        relevant = [event for event in events if window.contains(event.get("event_at"))]
        captures = [
            amount for event in relevant
            if event.get("event_type") == "captured" and event.get("status") == "confirmed"
            and (amount := _money(event.get("amount_brl"))) is not None
        ]
        anchored = [
            amount for event in relevant
            if event.get("event_type") == "captured" and event.get("status") == "confirmed"
            and _near(event.get("event_at"), approved_at, ANCHOR_WINDOW)
            and (amount := _money(event.get("amount_brl"))) is not None
        ]
        mismatches = [
            _money(event.get("amount_brl")) for event in relevant
            if event.get("event_type") == "reconciliation_mismatch"
            and event.get("status") == "open"
        ]

        refund_ev = await self.fetch("get_refund_timeline", order_id=order_id)
        refund_data = refund_ev.data if refund_ev and isinstance(refund_ev.data, dict) else {}
        refund_rows = [event for event in _as_list(refund_data.get("events"))
                       if event.get("order_id") in (None, order_id)]
        refunds = [event for event in refund_rows if window.contains(event.get("event_at"))]
        if refund_ev and refunds:
            evidence["refund"] = refund_ev
        findings = {
            "captures": captures,
            "anchored_captures": anchored,
            "open_mismatches": [amount for amount in mismatches if amount is not None],
            "refund_statuses": [str(event.get("status")) for event in refunds],
            "refund_amounts": [_money(event.get("amount_brl")) for event in refunds],
            "excluded_payment_events": len(events) - len(relevant),
            "excluded_refund_events": len(refund_rows) - len(refunds),
        }
        code = "PAYMENT_CONTEXT_READY" if payment_ev else "PAYMENT_EVIDENCE_MISSING"
        return self.handoff("payment_context", findings, evidence, code)


class ShipmentAgent(Agent):
    name = SHIPMENT_AGENT

    async def run(self, order_id: str, window: CaseWindow) -> Handoff:
        evidence: dict[str, Evidence] = {}
        shipment_ev = await self.fetch("get_shipment_summary", order_id=order_id)
        data = shipment_ev.data if shipment_ev and isinstance(shipment_ev.data, dict) else {}
        if shipment_ev:
            evidence["shipment"] = shipment_ev
        events = [event for event in _as_list(data.get("events"))
                  if event.get("order_id") in (None, order_id)]
        relevant = [event for event in events if window.contains(event.get("event_at"))]
        late_actors = sorted({
            str(event.get("actor")) for event in relevant
            if event.get("event_type") == "delivered_late" and event.get("status") == "confirmed"
        })
        findings = {
            "status": data.get("order_status"),
            "carrier_at": data.get("delivered_carrier_at"),
            "customer_at": data.get("delivered_customer_at"),
            "estimated_at": data.get("estimated_delivery_at"),
            "late_actors": late_actors,
            "excluded_shipment_events": len(events) - len(relevant),
        }
        code = "SHIPMENT_CONTEXT_READY" if shipment_ev else "SHIPMENT_EVIDENCE_MISSING"
        return self.handoff("shipment_context", findings, evidence, code)


def classify(order: dict[str, Any], payment: dict[str, Any],
             shipment: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Return the primary issue and source conflicts observed while deciding it."""
    conflicts: list[dict[str, Any]] = []
    if not order.get("found") or not order.get("primary_item"):
        return "insufficient_evidence", conflicts

    status = order.get("status")
    if shipment.get("status") and status and shipment["status"] != status:
        conflicts.append({
            "field": "order_status",
            "sources": ["get_order", "get_shipment_summary"],
            "selected_source": "get_order",
            "resolution_code": "PREFER_AUTHORITATIVE_ORDER_ROW",
        })
    captures: list[Decimal] = payment.get("captures", [])
    if status in ("canceled", "unavailable") and captures:
        return f"{status}_order_paid", conflicts

    refund_statuses = payment.get("refund_statuses", [])
    if "failed" in refund_statuses:
        return "refund_failed", conflicts
    if "pending" in refund_statuses:
        return "refund_pending", conflicts
    if payment.get("open_mismatches"):
        return "payment_mismatch", conflicts

    item = order["primary_item"]
    order_total = (_money(item.get("price")) or Decimal(0)) + (
        _money(item.get("freight_value")) or Decimal(0))
    charges = payment.get("anchored_captures") or captures
    if len(charges) >= 2 and sum(charges) == order_total:
        return "valid_split_payment", conflicts
    if len(charges) >= 2 and len(set(charges)) == 1 and sum(charges) > order_total:
        return "duplicate_charge", conflicts

    delivered = _ts(order.get("customer_at"))
    estimated = _ts(order.get("estimated_at"))
    if delivered and estimated and delivered > estimated:
        carrier = _ts(order.get("carrier_at"))
        limit = _ts(item.get("shipping_limit_date"))
        timeline_actor = "seller" if carrier and limit and carrier > limit else \
            "logistics_provider"
        event_actors = shipment.get("late_actors", [])
        if event_actors and timeline_actor not in event_actors:
            conflicts.append({
                "field": "late_delivery_actor",
                "sources": ["get_order+get_order_items", "get_shipment_summary.events"],
                "selected_source": "get_order+get_order_items",
                "resolution_code": "PREFER_TIMESTAMP_COMPARISON",
            })
        return ("late_delivery_seller" if timeline_actor == "seller"
                else "late_delivery_logistics"), conflicts
    return "unsupported_claim", conflicts


class PolicyAgent(Agent):
    name = POLICY_AGENT

    async def run(self, policy_version: str, issue: str, order: dict[str, Any],
                  payment: dict[str, Any]) -> Handoff:
        evidence: dict[str, Evidence] = {}
        policy_ev = await self.fetch("get_policy", policy_version=policy_version)
        rules = {}
        if policy_ev and isinstance(policy_ev.data, dict):
            evidence["policy"] = policy_ev
            rules = policy_ev.data.get("rules") or {}
        rule = rules.get(issue) if isinstance(rules, dict) else None
        if not isinstance(rule, dict):
            decision = {
                "case_status": "needs_investigation",
                "action": FALLBACK_ACTION,
                "refund": Decimal(0),
                "parties": [{"party_type": "unknown", "party_id": None}],
                "rule_found": False,
            }
        else:
            refund = _money(rule.get("refund_brl")) or Decimal(0)
            # Never refund more than the customer was actually charged inside the case window.
            charged = sum(payment.get("captures", []), Decimal(0))
            if charged and refund > charged:
                refund = charged
            parties = []
            for party in _as_list(rule.get("responsible_parties")):
                party_type = party.get("party_type", "unknown")
                if party_type == "seller":
                    # Policy seller ids are templates; bind the seller evidenced for this order.
                    sellers = order.get("seller_ids") or [None]
                    parties.extend({"party_type": "seller", "party_id": seller}
                                   for seller in sellers)
                else:
                    parties.append({"party_type": party_type, "party_id": party.get("party_id")})
            decision = {
                "case_status": rule.get("case_status", "needs_investigation"),
                "action": rule.get("recommended_action") or FALLBACK_ACTION,
                "refund": refund,
                "parties": parties or [{"party_type": "unknown", "party_id": None}],
                "rule_found": True,
            }
        self.trace.emit(
            case_id=self.case_id,
            event_type="policy_decided",
            actor=self.name,
            decision_code=issue,
            evidence_refs=[policy_ev.ref] if policy_ev else None,
            attributes={
                "case_status": decision["case_status"],
                "recommended_action": decision["action"],
                "refund_brl": float(decision["refund"]),
            },
        )
        return self.handoff("policy_decision", decision, evidence,
                            "POLICY_APPLIED" if decision["rule_found"] else "POLICY_RULE_MISSING")


class Verifier(Agent):
    name = VERIFIER

    def run(self, output: dict[str, Any], consumed_refs: set[str]) -> Handoff:
        failures: list[str] = []
        if output["case_id"] != self.case_id:
            failures.append("CASE_ID_MISMATCH")
        refs = output["evidence_refs"]
        if not refs or not set(refs) <= consumed_refs:
            failures.append("EVIDENCE_NOT_CONSUMED_IN_CASE")
        financial = output["financial_resolution"]
        line_total = sum((_money(line["amount_brl"]) for line in financial["refund_lines"]),
                         Decimal(0))
        if line_total != _money(financial["recommended_refund_brl"]):
            failures.append("REFUND_LINES_TOTAL_MISMATCH")
        status = output["assessment"]["case_status"]
        if status == "no_action" and financial["recommended_refund_brl"] > 0:
            failures.append("NO_ACTION_WITH_REFUND")
        if not output["resolution_actions"]:
            failures.append("MISSING_ACTION")
        parties = output["root_cause_analysis"]["responsible_parties"]
        if any(p["party_type"] == "seller" and p["party_id"] not in
               output["affected_entities"]["seller_ids"] for p in parties):
            failures.append("SELLER_NOT_IN_ENTITIES")
        if not 0 <= output["assessment"]["confidence"] <= 1:
            failures.append("CONFIDENCE_OUT_OF_BOUNDS")
        code = "VERIFIED" if not failures else failures[0]
        self.trace.emit(
            case_id=self.case_id,
            event_type="verification_completed",
            actor=self.name,
            decision_code=code,
            evidence_refs=refs[:20] or None,
            attributes={"checks_failed": len(failures)},
        )
        if failures:
            raise WorkflowError(f"{self.case_id}: verification failed: {', '.join(failures)}")
        return self.handoff("verification", {"result": code}, {}, code)


def _claim_verdict(topic: str, issue: str, refund: Decimal, status: str) -> str:
    if issue == "insufficient_evidence":
        return "insufficient_evidence"
    if topic == "requested_full_refund":
        if refund <= 0:
            return "insufficient_evidence" if status == "needs_investigation" else "unsupported"
        return "supported" if issue in FULL_REFUND_ISSUES else "partially_supported"
    if topic == issue:
        return "unsupported" if issue == "unsupported_claim" else "supported"
    return "unsupported"


def _confidence(issue: str, order: dict[str, Any], payment: dict[str, Any],
                conflicts: list[dict[str, Any]]) -> float:
    if issue == "insufficient_evidence":
        return 0.5
    confidence = 0.95
    if conflicts:
        confidence -= 0.1
    # Contaminating rows inside the case window make the selection less certain.
    if len(payment.get("captures", [])) > len(payment.get("anchored_captures", [])) or \
            len(order.get("item_ids", [])) > 1:
        confidence -= 0.05
    return round(confidence, 2)


def _data_conflicts(order: dict[str, Any], payment: dict[str, Any],
                    shipment: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts = []
    for name in order.get("conflicting_item_fields", []):
        conflicts.append({
            "field": f"order_items.{name}",
            "sources": ["get_order_items:in_case_window", "get_order_items:outside_case_window"],
            "selected_source": "get_order_items:in_case_window",
            "resolution_code": "PREFER_RECORD_WITHIN_CASE_WINDOW",
        })
    if payment.get("excluded_payment_events"):
        conflicts.append({
            "field": "payment_timeline.events",
            "sources": ["get_payment_timeline:in_case_window",
                        "get_payment_timeline:outside_case_window"],
            "selected_source": "get_payment_timeline:in_case_window",
            "resolution_code": "EXCLUDE_EVENTS_OUTSIDE_CASE_WINDOW",
        })
    return conflicts


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator: dispatch specialists, apply policy, verify and assemble the output."""
    case_id = case["case_id"]
    request = case.get("customer_request") or {}
    order_id = str(request.get("claimed_order_id") or "")
    claims = _as_list(request.get("claims"))
    opened_at = _ts(case.get("opened_at"))

    def assign(agent: str, task: str) -> None:
        trace.emit(case_id=case_id, event_type="task_assigned", actor=COORDINATOR,
                   target=agent, decision_code=task.upper())

    evidence: dict[str, Evidence] = {}

    assign(ORDER_AGENT, "order_context")
    order_msg = await OrderAgent(case_id, gateway, trace).run(order_id, opened_at)
    evidence.update(order_msg.evidence)
    order = order_msg.findings
    window = CaseWindow(_ts(order.get("purchase_at")), opened_at)

    payment: dict[str, Any] = {}
    shipment: dict[str, Any] = {}
    if order.get("found"):
        assign(PAYMENT_AGENT, "payment_context")
        payment_msg = await PaymentAgent(case_id, gateway, trace).run(
            order_id, window, order.get("approved_at"))
        evidence.update(payment_msg.evidence)
        payment = payment_msg.findings

        assign(SHIPMENT_AGENT, "shipment_context")
        shipment_msg = await ShipmentAgent(case_id, gateway, trace).run(order_id, window)
        evidence.update(shipment_msg.evidence)
        shipment = shipment_msg.findings

    issue, source_conflicts = classify(order, payment, shipment)

    assign(POLICY_AGENT, "policy_decision")
    policy_msg = await PolicyAgent(case_id, gateway, trace).run(
        str(case.get("policy_version") or ""), issue, order, payment)
    evidence.update(policy_msg.evidence)
    decision = policy_msg.findings

    seller_ids: list[str] = list(order.get("seller_ids", []))
    if issue in SELLER_RESPONSIBLE and seller_ids:
        assign(ORDER_AGENT, "seller_lookup")
        seller_msg = await OrderAgent(case_id, gateway, trace).run_seller_lookup(
            order_id, seller_ids)
        evidence.update(seller_msg.evidence)

    cited = [evidence[group].ref for group in CITED_GROUPS[issue] if group in evidence]
    refund: Decimal = decision["refund"]
    case_status = decision["case_status"]
    if refund > 0:
        entity = order_id if issue in FULL_ORDER_ISSUES or not order.get("item_ids") \
            else order["item_ids"][0]
        refund_lines = [{"reason_code": issue, "amount_brl": float(refund), "entity_id": entity}]
    else:
        refund_lines = []

    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": case_status,
            "confidence": _confidence(issue, order, payment, source_conflicts),
        },
        "affected_entities": {
            "order_ids": [order_id] if order.get("found") else [],
            "item_ids": list(order.get("item_ids", [])),
            "seller_ids": seller_ids,
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [
            {
                "claim_id": str(claim.get("claim_id")),
                "verdict": _claim_verdict(str(claim.get("topic")), issue, refund, case_status),
                "confidence": 0.9 if issue != "insufficient_evidence" else 0.5,
                "evidence_refs": cited,
            }
            for claim in claims[:5] if claim.get("claim_id")
        ],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": decision["parties"],
        },
        "evidence_refs": cited,
        "data_conflicts": (source_conflicts + _data_conflicts(order, payment, shipment))[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [decision["action"]],
    }

    assign(VERIFIER, "verification")
    consumed = {item.ref for item in evidence.values()}
    Verifier(case_id, gateway, trace).run(output, consumed)
    return output
