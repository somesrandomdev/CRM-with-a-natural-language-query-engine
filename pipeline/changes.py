"""Turning an extraction into proposed lead field changes, and applying them."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.models import Lead, Sentiment
from pipeline.extractor import Extraction

FIELD_LABELS = {
    "budget_usd": "Budget (USD)",
    "timeline": "Timeline",
    "objections": "Objections",
    "sentiment": "Sentiment",
}


@dataclass(frozen=True)
class FieldChange:
    field: str
    current: Any
    proposed: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label": FIELD_LABELS[self.field],
            "current": self.current,
            "proposed": self.proposed,
        }


def _json_number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def merge_objections(existing: list[str] | None, new: list[str]) -> list[str]:
    """Existing objections first, then new ones not already present (case-insensitive)."""
    merged = list(existing or [])
    seen = {o.casefold() for o in merged}
    for objection in new:
        if objection.casefold() not in seen:
            merged.append(objection)
            seen.add(objection.casefold())
    return merged


def compute_changes(lead: Lead, extraction: Extraction) -> list[FieldChange]:
    """The fields of `lead` that applying `extraction` would change. No-ops are omitted."""
    changes: list[FieldChange] = []
    if extraction.budget_hint is not None:
        proposed = Decimal(str(extraction.budget_hint)).quantize(Decimal("0.01"))
        if lead.budget_usd != proposed:
            changes.append(
                FieldChange("budget_usd", _json_number(lead.budget_usd), float(proposed))
            )
    if extraction.timeline is not None and lead.timeline != extraction.timeline:
        changes.append(FieldChange("timeline", lead.timeline, extraction.timeline))
    merged = merge_objections(lead.objections, extraction.objections)
    if merged != (lead.objections or []):
        changes.append(FieldChange("objections", list(lead.objections or []), merged))
    if lead.sentiment != extraction.sentiment:
        changes.append(
            FieldChange(
                "sentiment",
                lead.sentiment.value if lead.sentiment else None,
                extraction.sentiment.value,
            )
        )
    return changes


def apply_changes(lead: Lead, changes: list[FieldChange]) -> None:
    for change in changes:
        match change.field:
            case "budget_usd":
                lead.budget_usd = Decimal(str(change.proposed)).quantize(Decimal("0.01"))
            case "timeline":
                lead.timeline = change.proposed
            case "objections":
                lead.objections = list(change.proposed)
            case "sentiment":
                lead.sentiment = Sentiment(change.proposed)
            case _:  # pragma: no cover - guarded by FIELD_LABELS
                raise ValueError(f"unknown field {change.field!r}")
