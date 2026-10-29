"""In-memory backend for the demo support agent (fictional store: Larkspur Outdoor Co.).

Deterministic on purpose: the store's clock is frozen, so re-recording the same
scenarios produces the same tool results. Tools enforce their own business rules
(a refund over the eligible amount is rejected by the backend, not just by the
prompt), which is what you want from a real system too.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from typing import Any

from sparkgate.schemas import ToolCall, ToolDef

STORE_TODAY = date(2026, 10, 20)
RETURN_WINDOW_DAYS = 30
AUTO_REFUND_LIMIT = 200.00

_ORDERS: dict[str, dict[str, Any]] = {
    "LK-1001": {"email": "maya.r@example.com", "status": "delivered", "delivered": "2026-10-12",
                "items": [{"sku": "TENT-2P", "name": "Ridgeline 2P Tent", "price": 189.00,
                           "final_sale": False}], "total": 189.00},
    "LK-1002": {"email": "maya.r@example.com", "status": "delivered", "delivered": "2026-08-30",
                "items": [{"sku": "BOOT-W8", "name": "Talus Hiking Boot W8", "price": 145.00,
                           "final_sale": False}], "total": 145.00},
    "LK-1003": {"email": "dev.k@example.com", "status": "delivered", "delivered": "2026-10-15",
                "items": [{"sku": "JKT-RAIN-M", "name": "Squall Rain Jacket M", "price": 120.00,
                           "final_sale": True}], "total": 120.00},
    "LK-1004": {"email": "dev.k@example.com", "status": "in_transit", "delivered": None,
                "eta": "2026-10-23",
                "items": [{"sku": "STOVE-1", "name": "Ember Camp Stove", "price": 79.00,
                           "final_sale": False}], "total": 79.00},
    "LK-1005": {"email": "sam.o@example.com", "status": "delivered", "delivered": "2026-10-05",
                "items": [{"sku": "PACK-65", "name": "Cirque 65L Pack", "price": 289.00,
                           "final_sale": False}], "total": 289.00},
    "LK-1006": {"email": "sam.o@example.com", "status": "delivered", "delivered": "2026-10-18",
                "items": [{"sku": "LAMP-H", "name": "Lumen Headlamp", "price": 39.00,
                           "final_sale": False},
                          {"sku": "BTL-1L", "name": "Trail Bottle 1L", "price": 18.00,
                           "final_sale": False}], "total": 57.00},
    "LK-1007": {"email": "ana.p@example.com", "status": "lost", "delivered": None,
                "items": [{"sku": "BAG-0C", "name": "Glacier 0C Sleeping Bag", "price": 249.00,
                           "final_sale": False}], "total": 249.00},
    "LK-1008": {"email": "ana.p@example.com", "status": "delivered", "delivered": "2026-10-01",
                "items": [{"sku": "POLE-CF", "name": "Carbon Trek Poles", "price": 99.00,
                           "final_sale": False}], "total": 99.00, "refunded": 99.00},
    "LK-1009": {"email": "li.w@example.com", "status": "processing", "delivered": None,
                "items": [{"sku": "MAT-R5", "name": "Loft R5 Sleeping Mat", "price": 129.00,
                           "final_sale": False}], "total": 129.00},
}

_HELP = [
    {"id": "KB-returns", "title": "Returns and refunds",
     "body": "Unused items can be returned within 30 days of delivery for a full refund to the "
             "original payment method. Final-sale items cannot be returned. Refunds post in "
             "5-7 business days."},
    {"id": "KB-shipping", "title": "Shipping times",
     "body": "Standard shipping takes 3-6 business days. Tracking updates within 24 hours of "
             "dispatch. Orders in 'processing' have not shipped yet and can still be cancelled."},
    {"id": "KB-damaged", "title": "Damaged or defective items",
     "body": "Report damage within 14 days of delivery. A specialist reviews each claim and "
             "arranges a replacement or refund; please keep the packaging."},
    {"id": "KB-warranty", "title": "Warranty",
     "body": "Tents, packs and stoves carry a 2-year warranty against manufacturing defects. "
             "Warranty claims are handled by a specialist through a support ticket."},
]

TOOLS: list[ToolDef] = [
    ToolDef(
        name="lookup_order",
        description="Fetch an order by its id (format LK-0000). Returns status, items, totals, "
                    "delivery date and the email on the order.",
        parameters={"type": "object", "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"], "additionalProperties": False},
    ),
    ToolDef(
        name="check_refund_eligibility",
        description="Check whether an order can be refunded under the returns policy and the "
                    "maximum refundable amount. Must be called before issue_refund.",
        parameters={"type": "object", "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"], "additionalProperties": False},
    ),
    ToolDef(
        name="issue_refund",
        description="Issue a refund to the original payment method.",
        parameters={"type": "object",
                    "properties": {"order_id": {"type": "string"},
                                   "amount": {"type": "number", "exclusiveMinimum": 0},
                                   "reason": {"type": "string"}},
                    "required": ["order_id", "amount", "reason"], "additionalProperties": False},
    ),
    ToolDef(
        name="create_ticket",
        description="Open a ticket for a human specialist.",
        parameters={"type": "object",
                    "properties": {"order_id": {"type": ["string", "null"]},
                                   "category": {"type": "string",
                                                "enum": ["damaged", "lost", "billing",
                                                         "warranty", "high_value_refund",
                                                         "other"]},
                                   "priority": {"type": "string", "enum": ["low", "normal",
                                                                           "high"]},
                                   "summary": {"type": "string"}},
                    "required": ["category", "priority", "summary"],
                    "additionalProperties": False},
    ),
    ToolDef(
        name="search_help_center",
        description="Search help-center articles by keyword.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"], "additionalProperties": False},
    ),
]


def _days_since(iso: str | None) -> int | None:
    return None if iso is None else (STORE_TODAY - date.fromisoformat(iso)).days


class LiveBackend:
    """Executes tools against fresh in-memory state (one instance per conversation)."""

    def __init__(self) -> None:
        self.orders = copy.deepcopy(_ORDERS)
        self._refund_seq = 0
        self._ticket_seq = 0

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolCall:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None or name not in {t.name for t in TOOLS}:
            result: dict[str, Any] = {"error": "unknown_tool", "tool": name}
        else:
            try:
                result = handler(**arguments)
            except (TypeError, AttributeError, ValueError) as exc:
                result = {"error": "invalid_arguments", "detail": str(exc)[:200]}
        return ToolCall(name=name, arguments=arguments, result=json.dumps(result),
                        matched="live")

    # -- tools ---------------------------------------------------------------

    def _t_lookup_order(self, order_id: str) -> dict[str, Any]:
        o = self.orders.get(str(order_id).strip().upper())
        if o is None:
            return {"error": "order_not_found", "order_id": order_id}
        return {"order_id": order_id.upper(), **o,
                "days_since_delivery": _days_since(o.get("delivered"))}

    def _eligibility(self, order_id: str) -> dict[str, Any]:
        o = self.orders.get(str(order_id).strip().upper())
        if o is None:
            return {"eligible": False, "reason": "order_not_found", "max_refund": 0}
        if o.get("refunded"):
            return {"eligible": False, "reason": "already_refunded", "max_refund": 0}
        if o["status"] != "delivered":
            return {"eligible": False, "reason": f"order_status_{o['status']}", "max_refund": 0}
        days = _days_since(o["delivered"]) or 0
        if days > RETURN_WINDOW_DAYS:
            return {"eligible": False, "reason": "outside_30_day_window",
                    "days_since_delivery": days, "max_refund": 0}
        refundable = round(sum(i["price"] for i in o["items"] if not i["final_sale"]), 2)
        if refundable == 0:
            return {"eligible": False, "reason": "final_sale", "max_refund": 0}
        return {"eligible": True, "reason": "within_policy", "days_since_delivery": days,
                "max_refund": refundable,
                "requires_specialist": refundable > AUTO_REFUND_LIMIT}

    def _t_check_refund_eligibility(self, order_id: str) -> dict[str, Any]:
        return {"order_id": order_id, **self._eligibility(order_id)}

    def _t_issue_refund(self, order_id: str, amount: float, reason: str) -> dict[str, Any]:
        elig = self._eligibility(order_id)
        if not elig["eligible"]:
            return {"error": "refund_rejected", "reason": elig["reason"]}
        if amount > elig["max_refund"] + 1e-9:
            return {"error": "refund_rejected", "reason": "amount_exceeds_max_refund",
                    "max_refund": elig["max_refund"]}
        if amount > AUTO_REFUND_LIMIT:
            return {"error": "refund_rejected", "reason": "requires_specialist_approval"}
        self._refund_seq += 1
        self.orders[order_id.upper()]["refunded"] = amount
        return {"refund_id": f"RF-{7700 + self._refund_seq}", "order_id": order_id,
                "amount": amount, "status": "pending", "eta_business_days": "5-7"}

    def _t_create_ticket(self, category: str, priority: str, summary: str,
                         order_id: str | None = None) -> dict[str, Any]:
        self._ticket_seq += 1
        return {"ticket_id": f"TK-{5500 + self._ticket_seq}", "category": category,
                "priority": priority, "order_id": order_id, "status": "open",
                "first_response_hours": 4 if priority == "high" else 24}

    def _t_search_help_center(self, query: str) -> dict[str, Any]:
        words = {w for w in str(query).lower().split() if len(w) > 2}
        hits = [a for a in _HELP
                if words & set((a["title"] + " " + a["body"]).lower().replace(".", "").split())]
        return {"results": hits[:3]}
