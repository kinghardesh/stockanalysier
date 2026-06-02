from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import settings


class LLMTradeProposal(BaseModel):
    # protected_namespaces=() silences Pydantic v2's warning about the
    # `model_used` field colliding with its reserved `model_` namespace.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    ticker: str
    side: Literal["buy", "sell"]
    proposed_size_pct: float = Field(gt=0.0, le=0.05)
    stop_price: float = Field(gt=0)
    target_price: Optional[float] = Field(default=None, gt=0)
    thesis: str = Field(max_length=500)
    confidence: int = Field(ge=1, le=10)
    invalidation_criteria: str = Field(max_length=200)
    time_horizon: Literal["intraday", "swing", "position"]
    news_event_ids: list[str] = Field(default_factory=list)

    # Filled by orchestrator after the call, NOT by the LLM.
    model_used: Optional[str] = None
    tier: Optional[Literal["tier_1", "tier_2", "tier_3", "rejected_bear_case"]] = None
    sleeve: Literal["trend", "premium", "mean_reversion", "discretionary"] = "discretionary"
    # GPT tie-breaker verdict (orchestrator-filled). verdict is one of
    # "agree" / "disagree" / "error" / None (not run).
    verifier_verdict: Optional[str] = None
    verifier_model: Optional[str] = None
    verifier_confidence: Optional[int] = None
    verifier_reasoning: Optional[str] = None

    @field_validator("ticker")
    @classmethod
    def ticker_in_whitelist(cls, v: str) -> str:
        whitelist = {t.upper() for t in settings.whitelist_tickers}
        if not whitelist:
            raise ValueError("whitelist_tickers is empty; refusing to validate")
        if v.upper() not in whitelist:
            raise ValueError(f"ticker {v!r} not in whitelist")
        return v.upper()


class BearCaseAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strength: Literal["very_weak", "weak", "moderate", "strong", "very_strong"]
    reasoning: str = Field(max_length=500)
    key_risks: list[str] = Field(default_factory=list, max_length=5)


class CandidateAssessment(BaseModel):
    """LLM verdict on a mechanically-screened buy candidate (not whitelist-gated:
    the ticker is already a vetted universe member from the screen)."""
    model_config = ConfigDict(extra="forbid")

    buy: bool
    confidence: int = Field(ge=1, le=10)
    thesis: str = Field(max_length=500)
    stop_price: Optional[float] = Field(default=None, gt=0)
    target_price: Optional[float] = Field(default=None, gt=0)
    time_horizon: Literal["intraday", "swing", "position"] = "swing"


class VerifierVerdict(BaseModel):
    """Independent second-opinion verdict from the GPT tie-breaker."""
    model_config = ConfigDict(extra="forbid")

    agree: bool
    confidence: int = Field(ge=1, le=10)
    reasoning: str = Field(max_length=400)


def clean_schema_for_gemini(schema: dict) -> dict:
    """Make a Pydantic-emitted JSON schema acceptable to Gemini's response_schema.

    Gemini accepts only an OpenAPI 3.0 subset and rejects several patterns Pydantic
    emits by default. This walker handles all four documented failure modes:

      1. `$ref` indirection — inlined from `$defs`/`definitions`.
      2. `$defs`, `definitions`, `title` — stripped at every nesting level.
      3. `additionalProperties: false` (from `extra="forbid"`) — stripped.
      4. `anyOf: [X, {type:null}]` (from `Optional[X]`) — flattened to X.

    If you ever see `gemini unexpected: Unknown field for Schema: <name>` again,
    add the offending key to the strip set below.
    """
    defs = {**(schema.get("$defs", {}) or {}), **(schema.get("definitions", {}) or {})}
    # Keys Gemini's response_schema validator rejects. Empirically discovered;
    # add to this set every time a new failure shows up in the worker logs.
    # We rely on Pydantic to enforce numeric bounds AFTER the LLM responds —
    # Gemini's lack of in-schema enforcement just means more validation
    # failures on the retry path, which still beats silent acceptance.
    STRIP_KEYS = {
        "$defs", "definitions", "title", "additionalProperties",
        "exclusiveMinimum", "exclusiveMaximum",
        "minimum", "maximum",
        # Pydantic emits `default: <value>` for every field with a default
        # (Optional fields, list defaults, etc.). Gemini rejects it.
        "default",
        # Gemini's protobuf Schema literally has no maxLength/minLength
        # fields. Pydantic emits these for Field(max_length=...). Stripping
        # them means Pydantic still enforces the bound on the response,
        # but Gemini never sees the constraint.
        "maxLength", "minLength",
        # Less common but safe to defensively strip — discovered as Gemini
        # rejections in other projects.
        "examples", "pattern", "format",
    }

    def walk(node):
        if isinstance(node, dict):
            # 1. Inline $ref
            if "$ref" in node and len(node) == 1:
                name = node["$ref"].split("/")[-1]
                if name in defs:
                    return walk(defs[name])
                return {}

            # 4. Flatten anyOf with a null variant: Optional[X] -> X
            if "anyOf" in node and isinstance(node["anyOf"], list):
                non_null = [
                    v for v in node["anyOf"]
                    if not (isinstance(v, dict) and v.get("type") == "null")
                ]
                if len(non_null) == 1 and len(non_null) < len(node["anyOf"]):
                    rest = {k: v for k, v in node.items() if k != "anyOf"}
                    merged = {**non_null[0], **rest}
                    return walk(merged)

            # 2 + 3. Strip unsupported keys at this level.
            return {k: walk(v) for k, v in node.items() if k not in STRIP_KEYS}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def llm_facing_schema(model: type[BaseModel], strip_fields: Optional[list[str]] = None) -> dict:
    raw = model.model_json_schema()
    strip = set(strip_fields or [])
    if "properties" in raw:
        raw["properties"] = {k: v for k, v in raw["properties"].items() if k not in strip}
    if "required" in raw:
        raw["required"] = [r for r in raw["required"] if r not in strip]
    return clean_schema_for_gemini(raw)


_ORCH_FILLED = [
    "model_used", "tier", "sleeve",
    "verifier_verdict", "verifier_model", "verifier_confidence", "verifier_reasoning",
]


def llm_facing_news_batch_schema() -> dict:
    item = llm_facing_schema(LLMTradeProposal, strip_fields=_ORCH_FILLED)
    return clean_schema_for_gemini({
        "type": "object",
        "properties": {"proposals": {"type": "array", "items": item}},
        "required": ["proposals"],
    })


def llm_facing_filing_schema() -> dict:
    item = llm_facing_schema(LLMTradeProposal, strip_fields=_ORCH_FILLED)
    return clean_schema_for_gemini({
        "type": "object",
        "properties": {
            "proposal": item,
            "no_signal": {"type": "boolean"},
        },
        "required": ["no_signal"],
    })


def bear_case_schema() -> dict:
    return llm_facing_schema(BearCaseAssessment)


def verifier_schema() -> dict:
    return llm_facing_schema(VerifierVerdict)


def candidate_assessment_schema() -> dict:
    return llm_facing_schema(CandidateAssessment)
