from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ProposalSide, ProposalTier, TradeSleeve


class ProposalIn(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    signal_id: UUID
    ticker: str
    side: ProposalSide
    entry_price: Decimal
    stop_price: Optional[Decimal] = None
    target_price: Optional[Decimal] = None
    thesis: str
    confidence: int = Field(ge=1, le=10)
    model_used: Optional[str] = None
    tier: ProposalTier
    sleeve: TradeSleeve


class SizedProposal(ProposalIn):
    qty: int


class NewsItem(BaseModel):
    source: str
    source_ref: Optional[str] = None
    ticker: str
    title: str
    description: Optional[str] = None
    url: Optional[str] = None
    published_at: Optional[datetime] = None


class FilingItem(BaseModel):
    ticker: str
    cik: str
    form_type: str
    accession: str
    filed_at: datetime
    url: str
