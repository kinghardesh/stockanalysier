import re
from dataclasses import dataclass

IMPERATIVE_VERBS = {
    "buy", "buys", "buying", "bought",
    "sell", "sells", "selling", "sold",
    "short", "shorts", "shorting", "shorted",
    "dump", "dumps", "dumping", "dumped",
    "hold", "holds", "holding", "held",
}

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)", re.IGNORECASE),
    re.compile(r"system\s*:", re.IGNORECASE),
    re.compile(r"\b(you\s+are\s+now|new\s+instructions?|act\s+as)\b", re.IGNORECASE),
    re.compile(r"</?(system|assistant|user|prompt|instructions?)>", re.IGNORECASE),
    re.compile(r"```\s*system", re.IGNORECASE),
]

TICKER_PROXIMITY_TOKENS = 10
TOKEN_RE = re.compile(r"\S+")
WORD_RE = re.compile(r"[A-Za-z]+")
REDACTION = "[REDACTED]"


@dataclass
class SanitizationResult:
    sanitized: str
    stripped: list[dict]


def sanitize(text: str, tickers: list[str]) -> SanitizationResult:
    if not text:
        return SanitizationResult(sanitized="", stripped=[])

    stripped: list[dict] = []
    tokens = list(TOKEN_RE.finditer(text))
    token_strs = [t.group() for t in tokens]
    upper_tickers = {t.upper() for t in tickers}

    redact_indices: set[int] = set()
    for i, tok in enumerate(token_strs):
        if WORD_RE.fullmatch(tok) and tok.upper() in upper_tickers:
            lo = max(0, i - TICKER_PROXIMITY_TOKENS)
            hi = min(len(token_strs), i + TICKER_PROXIMITY_TOKENS + 1)
            for j in range(lo, hi):
                if j == i:
                    continue
                word_match = WORD_RE.search(token_strs[j])
                if word_match and word_match.group().lower() in IMPERATIVE_VERBS:
                    redact_indices.add(j)
                    stripped.append({
                        "fragment": token_strs[j],
                        "reason": f"imperative within {TICKER_PROXIMITY_TOKENS} tokens of ticker {tok}",
                    })

    if redact_indices:
        new_tokens = list(token_strs)
        for j in redact_indices:
            new_tokens[j] = REDACTION
        out = []
        last = 0
        for tok, m, replacement in zip(token_strs, tokens, new_tokens):
            out.append(text[last:m.start()])
            out.append(replacement)
            last = m.end()
        out.append(text[last:])
        sanitized = "".join(out)
    else:
        sanitized = text

    for pat in INJECTION_PATTERNS:
        for m in pat.finditer(sanitized):
            stripped.append({"fragment": m.group(), "reason": f"injection_pattern:{pat.pattern}"})
        sanitized = pat.sub(REDACTION, sanitized)

    return SanitizationResult(sanitized=sanitized, stripped=stripped)
