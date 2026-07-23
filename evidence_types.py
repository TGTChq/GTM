"""Structured facts and evidence used by every qualification gate."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class EvidenceStatus(str, Enum):
    VERIFIED_OFFICIAL = "VERIFIED_OFFICIAL"
    VERIFIED_CROSS_SOURCE = "VERIFIED_CROSS_SOURCE"
    PROVIDER_STRUCTURED_REVIEW = "PROVIDER_STRUCTURED_REVIEW"
    WEAK_PROVIDER_SIGNAL = "WEAK_PROVIDER_SIGNAL"
    CONTRADICTED = "CONTRADICTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class EvidenceItem:
    field: str
    value: Any
    status: EvidenceStatus | str
    source_type: str
    source_url: str = ""
    excerpt: str = ""
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["status"] = (
            self.status.value if isinstance(self.status, EvidenceStatus) else str(self.status)
        )
        return payload


@dataclass
class FactValue:
    name: str
    value: Any = None
    status: EvidenceStatus | str = EvidenceStatus.UNKNOWN
    evidence: List[EvidenceItem] = field(default_factory=list)
    contradictions: List[EvidenceItem] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        value = self.status.value if isinstance(self.status, EvidenceStatus) else str(self.status)
        return value in {
            EvidenceStatus.VERIFIED_OFFICIAL.value,
            EvidenceStatus.VERIFIED_CROSS_SOURCE.value,
            EvidenceStatus.PROVIDER_STRUCTURED_REVIEW.value,
        }

    @property
    def unknown(self) -> bool:
        value = self.status.value if isinstance(self.status, EvidenceStatus) else str(self.status)
        return value == EvidenceStatus.UNKNOWN.value

    @property
    def contradicted(self) -> bool:
        value = self.status.value if isinstance(self.status, EvidenceStatus) else str(self.status)
        return value == EvidenceStatus.CONTRADICTED.value or bool(self.contradictions)

    def to_dict(self) -> Dict[str, Any]:
        status = self.status.value if isinstance(self.status, EvidenceStatus) else str(self.status)
        return {
            "name": self.name,
            "value": self.value,
            "status": status,
            "verified": self.verified,
            "evidence": [item.to_dict() for item in self.evidence],
            "contradictions": [item.to_dict() for item in self.contradictions],
        }


@dataclass
class EvidenceBundle:
    facts: Dict[str, FactValue] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def add(self, fact: FactValue) -> None:
        self.facts[fact.name] = fact

    def get(self, name: str) -> Optional[FactValue]:
        return self.facts.get(name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "facts": {name: fact.to_dict() for name, fact in self.facts.items()},
            "notes": list(self.notes),
        }
