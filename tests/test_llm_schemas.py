import pytest
from pydantic import BaseModel

from app.llm.schemas import (
    LLMTradeProposal, bear_case_schema, clean_schema_for_gemini,
    llm_facing_filing_schema, llm_facing_news_batch_schema,
)


class _Nested(BaseModel):
    name: str


class _Wrapper(BaseModel):
    nested: _Nested


def _walk_assert_keys_absent(node, forbidden):
    if isinstance(node, dict):
        for k in forbidden:
            assert k not in node, f"found {k!r} in cleaned schema"
        for v in node.values():
            _walk_assert_keys_absent(v, forbidden)
    elif isinstance(node, list):
        for v in node:
            _walk_assert_keys_absent(v, forbidden)


def test_clean_schema_strips_defs_and_title_recursively():
    raw = _Wrapper.model_json_schema()
    assert "$defs" in raw
    cleaned = clean_schema_for_gemini(raw)
    _walk_assert_keys_absent(cleaned, {"$defs", "definitions", "title"})


def test_clean_schema_inlines_refs():
    raw = _Wrapper.model_json_schema()
    cleaned = clean_schema_for_gemini(raw)
    nested = cleaned["properties"]["nested"]
    assert "$ref" not in nested
    assert nested["properties"]["name"]["type"] == "string"


def test_llm_facing_schema_strips_orchestrator_fields():
    schema = llm_facing_filing_schema()
    item = schema["properties"]["proposal"]
    assert "model_used" not in item["properties"]
    assert "tier" not in item["properties"]


def test_news_batch_schema_shape():
    schema = llm_facing_news_batch_schema()
    assert schema["properties"]["proposals"]["type"] == "array"
    item = schema["properties"]["proposals"]["items"]
    assert "ticker" in item["properties"]


def test_bear_case_schema_strength_enum():
    schema = bear_case_schema()
    strength = schema["properties"]["strength"]
    assert set(strength["enum"]) == {"very_weak", "weak", "moderate", "strong", "very_strong"}
