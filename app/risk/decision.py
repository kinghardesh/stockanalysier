from dataclasses import dataclass, field
from typing import Union

from app.schemas import SizedProposal


@dataclass(frozen=True)
class Approved:
    proposal: SizedProposal


@dataclass(frozen=True)
class Rejected:
    reason: str
    detail: dict = field(default_factory=dict)


RiskDecision = Union[Approved, Rejected]
