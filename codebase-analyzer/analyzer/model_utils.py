"""Model name utilities — validation, aliasing, and graceful error handling.

This module is the single source of truth for:
  - Mapping deprecated/short model names to current API strings
  - Validating a model name before sending to the API (avoiding 404s)
  - Wrapping API errors in user-friendly messages
"""
import re
from typing import Optional, Tuple

# ── Canonical model names (as of June 2026) ───────────────────────────────
CURRENT_MODELS = {
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
}

# ── Aliases: any of these → the canonical replacement ─────────────────────
# Old date-suffix format, short names, common typos
MODEL_ALIASES: dict[str, str] = {
    # Deprecated Sonnet 4 (retired June 15 2026)
    "claude-sonnet-4-20250514":   "claude-sonnet-4-6",
    "claude-sonnet-4":            "claude-sonnet-4-6",
    "claude-3-5-sonnet-20241022": "claude-sonnet-4-6",
    "claude-3-7-sonnet-20250219": "claude-sonnet-4-6",
    # Deprecated Haiku
    "claude-haiku-4-5-20251001":  "claude-haiku-4-5",
    "claude-3-5-haiku-20241022":  "claude-haiku-4-5",
    "claude-haiku-4-5-0":         "claude-haiku-4-5",
    # Deprecated Opus 4
    "claude-opus-4-20250514":     "claude-opus-4-8",
    "claude-opus-4":              "claude-opus-4-8",
    "claude-opus-4-1":            "claude-opus-4-8",
}

# Default fallback when everything else fails
DEFAULT_MODEL = "claude-sonnet-4-6"


def resolve_model(name: str) -> Tuple[str, bool]:
    """Return (resolved_name, was_aliased).

    Always returns a usable model name — falls back to DEFAULT_MODEL
    if the input is unknown rather than crashing.
    """
    if not name or not name.strip():
        return DEFAULT_MODEL, True
    name = name.strip()
    if name in CURRENT_MODELS:
        return name, False
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name], True
    # Heuristic: if it contains a date suffix like -20250514, strip it
    date_stripped = re.sub(r"-202\d{5}$", "", name)
    if date_stripped != name:
        if date_stripped in CURRENT_MODELS:
            return date_stripped, True
        if date_stripped in MODEL_ALIASES:
            return MODEL_ALIASES[date_stripped], True
    # Unknown but looks like a real model name — pass through and let the
    # API reject it with a clear error rather than silently substituting
    return name, False


def friendly_api_error(exc: Exception, model: str) -> str:
    """Turn an Anthropic API exception into a clear, actionable message."""
    msg = str(exc)
    code = getattr(exc, "status_code", None) or ""

    if "404" in msg or code == 404 or "not_found_error" in msg:
        resolved, _ = resolve_model(model)
        hint = (f"  → Did you mean `{resolved}`?" if resolved != model else "")
        return (
            f"**Model not found:** `{model}` does not exist or has been retired.\n\n"
            f"{hint}\n\n"
            f"Update `LLM_MODEL` in your `.env` file to one of:\n"
            + "\n".join(f"- `{m}`" for m in sorted(CURRENT_MODELS))
            + "\n\n_The default (`claude-sonnet-4-6`) works for most use cases._"
        )
    if "401" in msg or code == 401 or "authentication_error" in msg:
        return (
            "**Invalid API key.** Your `ANTHROPIC_API_KEY` was rejected.\n\n"
            "- Check the key in the sidebar or your `.env` file\n"
            "- Keys start with `sk-ant-`\n"
            "- Generate a new key at [console.anthropic.com](https://console.anthropic.com)"
        )
    if "429" in msg or code == 429 or "rate_limit" in msg.lower():
        return (
            "**Rate limit hit.** Too many requests to the Anthropic API.\n\n"
            "- Wait 30–60 seconds and try again\n"
            "- Use `claude-haiku-4-5` for lower-cost, higher-rate-limit analysis\n"
            "- Results from completed chunks are cached — a retry only re-runs failed chunks"
        )
    if "529" in msg or "overloaded" in msg.lower():
        return (
            "**API overloaded.** Anthropic's servers are under high load.\n\n"
            "- Try again in a minute\n"
            "- The heuristic extractor runs for free without an API key if you need results now"
        )
    if "500" in msg or code == 500:
        return (
            "**Anthropic server error (500).** This is on Anthropic's side.\n\n"
            "- Try again in a moment\n"
            "- Check [status.anthropic.com](https://status.anthropic.com) for outages"
        )
    # Generic fallback
    return f"**LLM API error:** {msg[:300]}"
