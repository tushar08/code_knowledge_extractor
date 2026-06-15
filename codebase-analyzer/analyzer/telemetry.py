"""Token telemetry — lightweight usage tracking across all LLM calls.

All LLM calls (pipeline map/reduce + Q&A synthesis) report here.
The session ledger is kept in-memory and optionally persisted to a JSON file.

Usage:
    from analyzer.telemetry import telemetry
    telemetry.record("map", module="services.catalog",
                     input_tokens=450, output_tokens=210, cached=False)
    print(telemetry.summary())
"""
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

# Approximate cost per 1M tokens (claude-sonnet-4, June 2025 pricing)
COST_PER_1M = {
    "input":  3.00,   # USD
    "output": 15.00,
}


@dataclass
class TokenEvent:
    call_type: str        # "map" | "reduce" | "qa" | "heuristic"
    module: str = ""      # module name for map calls
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False  # True when served from DiskCache (zero API cost)
    timestamp: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float:
        if self.cached:
            return 0.0
        return (self.input_tokens  * COST_PER_1M["input"] +
                self.output_tokens * COST_PER_1M["output"]) / 1_000_000


@dataclass
class Telemetry:
    events: List[TokenEvent] = field(default_factory=list)
    _persist_path: Optional[Path] = field(default=None, repr=False)

    # ── Record ────────────────────────────────────────────────────────────
    def record(self, call_type: str, module: str = "",
               input_tokens: int = 0, output_tokens: int = 0,
               cached: bool = False):
        ev = TokenEvent(call_type=call_type, module=module,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cached=cached)
        self.events.append(ev)
        if self._persist_path:
            self._flush()
        return ev

    def reset(self):
        self.events.clear()
        if self._persist_path and self._persist_path.exists():
            self._persist_path.unlink()

    # ── Aggregates ────────────────────────────────────────────────────────
    @property
    def total_input_tokens(self) -> int:
        return sum(e.input_tokens for e in self.events if not e.cached)

    @property
    def total_output_tokens(self) -> int:
        return sum(e.output_tokens for e in self.events if not e.cached)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cost_usd(self) -> float:
        return sum(e.cost_usd for e in self.events)

    @property
    def cache_hits(self) -> int:
        return sum(1 for e in self.events if e.cached)

    @property
    def api_calls(self) -> int:
        return sum(1 for e in self.events if not e.cached)

    @property
    def estimated_tokens_saved(self) -> int:
        """Tokens that would have been used if cache hadn't been hit.
        We can't know the exact count, so we use the avg of live calls."""
        live = [e for e in self.events if not e.cached]
        if not live:
            return 0
        avg = sum(e.total_tokens for e in live) / len(live)
        return int(avg * self.cache_hits)

    def by_type(self) -> dict:
        buckets: dict = {}
        for e in self.events:
            b = buckets.setdefault(e.call_type, {"calls": 0, "input": 0,
                                                  "output": 0, "cached": 0,
                                                  "cost_usd": 0.0})
            b["calls"] += 1
            b["input"] += e.input_tokens
            b["output"] += e.output_tokens
            b["cached"] += 1 if e.cached else 0
            b["cost_usd"] += e.cost_usd
        return buckets

    def summary(self) -> dict:
        return {
            "api_calls":            self.api_calls,
            "cache_hits":           self.cache_hits,
            "total_input_tokens":   self.total_input_tokens,
            "total_output_tokens":  self.total_output_tokens,
            "total_tokens":         self.total_tokens,
            "est_tokens_saved_by_cache": self.estimated_tokens_saved,
            "total_cost_usd":       round(self.total_cost_usd, 6),
            "by_call_type":         self.by_type(),
        }

    def events_list(self) -> list:
        return [asdict(e) for e in self.events]

    # ── Persistence ───────────────────────────────────────────────────────
    def set_persist_path(self, path: str):
        self._persist_path = Path(path)
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        # Load existing events
        if self._persist_path.exists():
            try:
                data = json.loads(self._persist_path.read_text())
                self.events = [TokenEvent(**e) for e in data.get("events", [])]
            except Exception:
                pass

    def _flush(self):
        if self._persist_path:
            self._persist_path.write_text(
                json.dumps({"events": self.events_list(),
                            "summary": self.summary()}, indent=2))


# Module-level singleton shared by all components
telemetry = Telemetry()
