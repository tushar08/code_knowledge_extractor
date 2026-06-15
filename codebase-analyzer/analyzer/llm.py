"""LLM integration (LangChain + Anthropic Claude).

Design:
- Map step:    each token-bounded chunk of class digests -> ChunkInsights
- Reduce step: all chunk insights + README/build context  -> ProjectOverview

Structured output is enforced with `with_structured_output(pydantic_model)`,
which uses Anthropic tool-calling under the hood, guaranteeing parseable JSON.

A disk cache keyed by content hash makes re-runs idempotent and avoids
re-spending tokens on unchanged code.

Token usage is recorded to the module-level `telemetry` singleton after each
live API call via LangChain's usage_metadata on the raw AIMessage.
"""
import hashlib
import json
import os
from pathlib import Path
from typing import List, Optional

from .config import AnalyzerConfig
from .schemas import ChunkInsights, ClassInsight, MethodInsight, ProjectOverview
from .telemetry import telemetry
from .model_utils import resolve_model, friendly_api_error


class LLMUnavailableError(RuntimeError):
    """Raised when the LLM API is unreachable or rejects the request.
    The message is already formatted for display to the user."""
    pass


MAP_SYSTEM = (
    "You are a senior software architect analyzing a codebase. You receive a "
    "structural digest of Java classes (signatures, annotations, complexity). "
    "For each class produce a crisp summary and a one-sentence description per "
    "method, grounded strictly in the digest. Also record notable design "
    "observations. Respond only via the structured schema."
)

REDUCE_SYSTEM = (
    "You are a senior software architect. Using the project README/build files "
    "and the per-module observations gathered from the code, produce a "
    "high-level overview of the project's purpose, functionality, architecture, "
    "technology stack, design patterns, and noteworthy aspects (including "
    "complexity findings). Be concrete and grounded; respond only via the "
    "structured schema."
)


class DiskCache:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha256("||".join(parts).encode()).hexdigest()[:24]

    def get(self, key: str):
        return self.data.get(key)

    def put(self, key: str, value):
        self.data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))


def _extract_usage(raw_response) -> dict:
    """Pull input/output token counts from a LangChain AIMessage."""
    usage = {}
    if hasattr(raw_response, "usage_metadata") and raw_response.usage_metadata:
        m = raw_response.usage_metadata
        usage["input_tokens"] = getattr(m, "input_tokens", 0) or 0
        usage["output_tokens"] = getattr(m, "output_tokens", 0) or 0
    elif hasattr(raw_response, "response_metadata"):
        rm = raw_response.response_metadata or {}
        usage_block = rm.get("usage", rm.get("token_usage", {}))
        usage["input_tokens"]  = usage_block.get("input_tokens",
                                  usage_block.get("prompt_tokens", 0))
        usage["output_tokens"] = usage_block.get("output_tokens",
                                  usage_block.get("completion_tokens", 0))
    return usage


class ClaudeExtractor:
    """LangChain-based extractor backed by Anthropic Claude."""

    def __init__(self, cfg: AnalyzerConfig, cache: DiskCache):
        from langchain_anthropic import ChatAnthropic  # lazy import
        self.cfg = cfg
        self.cache = cache
        # Resolve the model name defensively before hitting the API
        resolved, was_aliased = resolve_model(cfg.model_name)
        if was_aliased:
            print(f"[model] Auto-corrected '{cfg.model_name}' → '{resolved}'")
        self.model_name = resolved
        self.llm = ChatAnthropic(
            model=self.model_name,
            temperature=cfg.temperature,
            max_tokens=cfg.max_output_tokens,
            api_key=cfg.anthropic_api_key,
        )
        self._raw_llm = self.llm
        self.map_llm    = self.llm.with_structured_output(ChunkInsights)
        self.reduce_llm = self.llm.with_structured_output(ProjectOverview)

    def analyze_chunk(self, module: str, digest_text: str) -> ChunkInsights:
        cache_key = self.cache.key("map", self.model_name, digest_text)
        if (hit := self.cache.get(cache_key)) is not None:
            telemetry.record("map", module=module, cached=True)
            return ChunkInsights.model_validate(hit)

        result = self._invoke_map(module, digest_text)
        self.cache.put(cache_key, result.model_dump())
        return result

    def _is_max_tokens_error(self, exc: Exception) -> bool:
        """Return True when failure is output truncation, not an API or auth error."""
        msg = str(exc).lower()
        # LangChain surfaces max_tokens as a ValidationError on an empty dict {}
        # because the JSON was cut off mid-stream.
        # It can also return None silently for "parsed" without raising — we guard
        # against that upstream and raise a ValueError with "returned none".
        return (
            "max_tokens" in msg
            or ("field required" in msg and "classes" in msg)
            or ("validation error" in msg and "chunksights" in msg.lower())
            or "returned none" in msg
            or "schema not satisfied" in msg
            or "truncated" in msg
        )

    def _invoke_map(self, module: str, digest_text: str,
                    depth: int = 0) -> ChunkInsights:
        """Invoke the map step with automatic split-and-retry on truncation.

        depth=0  normal call
        depth=1  first retry: chunk was split in half
        depth=2  second retry: quarter-chunks — gives up after this
        """
        MAX_DEPTH = 2
        messages = [
            ("system", MAP_SYSTEM),
            ("human", f"Module: {module}\n\nClass digests:\n\n{digest_text}"),
        ]
        try:
            raw_chain = self.llm.with_structured_output(ChunkInsights, include_raw=True)
            raw = raw_chain.invoke(messages)
            result: ChunkInsights = raw.get("parsed") if isinstance(raw, dict) else None

            # Guard: LangChain can return None for "parsed" when the model output
            # doesn't match the schema (e.g. max_tokens truncation without raising,
            # or the tool-call JSON was malformed). Treat it like a max_tokens error.
            if result is None:
                raise ValueError(
                    "Structured output parser returned None — output may have been "
                    "truncated (max_tokens) or the schema was not satisfied."
                )

            usage = _extract_usage(raw.get("raw") if isinstance(raw, dict) else None)
            telemetry.record("map", module=module, cached=False, **usage)
            return result

        except Exception as exc:
            if self._is_max_tokens_error(exc) and depth < MAX_DEPTH:
                # Output was truncated — split the digest in half and retry each piece
                print(f"    [retry] max_tokens on chunk [{module}] "
                      f"(depth={depth}) — splitting and retrying ...")
                from .chunker import split_chunk
                fake_chunk = {"module": module, "text": digest_text,
                              "tokens": self.cfg.estimate_tokens(digest_text)}
                sub_chunks = split_chunk(fake_chunk, self.cfg)

                if len(sub_chunks) <= 1:
                    # Already at minimum granularity — fall back to heuristic
                    print(f"    [warn] Cannot split further — using heuristic for [{module}]")
                    return HeuristicExtractor(self.cfg).analyze_chunk(module, digest_text)

                # Process each sub-chunk and merge results
                merged_classes: list = []
                merged_obs: list = []
                for sub in sub_chunks:
                    sub_result = self._invoke_map(module, sub["text"], depth + 1)
                    merged_classes.extend(sub_result.classes)
                    merged_obs.extend(sub_result.module_observations)
                return ChunkInsights(classes=merged_classes,
                                     module_observations=merged_obs)

            raise LLMUnavailableError(friendly_api_error(exc, self.model_name)) from exc

    def synthesize_overview(self, context_files: dict,
                            observations: List[str],
                            metrics_json: str) -> ProjectOverview:
        context = "\n\n".join(f"=== {k} ===\n{v[:6000]}" for k, v in context_files.items())
        obs = "\n".join(f"- {o}" for o in observations)
        payload = (f"{context}\n\n=== Aggregated metrics ===\n{metrics_json}\n\n"
                   f"=== Per-module observations from code analysis ===\n{obs}")
        cache_key = self.cache.key("reduce", self.model_name, payload)
        if (hit := self.cache.get(cache_key)) is not None:
            telemetry.record("reduce", cached=True)
            return ProjectOverview.model_validate(hit)

        messages = [("system", REDUCE_SYSTEM), ("human", payload)]
        try:
            raw_chain = self.llm.with_structured_output(ProjectOverview, include_raw=True)
            raw = raw_chain.invoke(messages)
            result: ProjectOverview = raw.get("parsed") if isinstance(raw, dict) else None
            if result is None:
                raise ValueError(
                    "Structured output parser returned None for ProjectOverview — "
                    "output may have been truncated or schema not satisfied."
                )
            usage = _extract_usage(raw.get("raw") if isinstance(raw, dict) else None)
            telemetry.record("reduce", cached=False, **usage)
            self.cache.put(cache_key, result.model_dump())
            return result
        except Exception as exc:
            # Raise LLMUnavailableError so main.py can catch it cleanly,
            # log a warning, and continue with an empty overview rather than crashing.
            raise LLMUnavailableError(friendly_api_error(exc, self.model_name)) from exc


class HeuristicExtractor:
    """Offline fallback used when ANTHROPIC_API_KEY is absent."""

    VERBS = {"get": "Retrieves", "add": "Creates", "create": "Creates",
             "update": "Updates", "delete": "Deletes", "find": "Finds",
             "search": "Searches for", "login": "Authenticates", "to": "Converts to"}

    def __init__(self, cfg: AnalyzerConfig, cache: Optional[DiskCache] = None):
        self.cfg = cfg

    def _describe_method(self, name: str, sig: str) -> str:
        for prefix, verb in self.VERBS.items():
            if name.startswith(prefix):
                subject = name[len(prefix):] or "resource"
                spaced = "".join((" " + c.lower()) if c.isupper() else c
                                 for c in subject).strip()
                return f"{verb} {spaced}."
        return f"Implements `{name}` as declared: {sig}."

    def analyze_chunk(self, module: str, digest_text: str) -> ChunkInsights:
        telemetry.record("heuristic", module=module, cached=False)
        classes = []
        for block in digest_text.split("\n\n"):
            lines = block.splitlines()
            qn, methods = None, []
            for line in lines:
                if line.startswith(("CLASS:", "INTERFACE:", "ENUM:")):
                    qn = line.split(":", 1)[1].split("[")[0].strip()
                if line.strip().startswith("- "):
                    sig = line.strip()[2:].split(" (cc=")[0]
                    name = sig.split("(")[0].split()[-1]
                    methods.append(MethodInsight(name=name,
                                                 description=self._describe_method(name, sig)))
            if qn:
                classes.append(ClassInsight(
                    qualified_name=qn,
                    summary=f"Member of module {module}; responsibility inferred "
                            f"from its name and annotations.",
                    methods=methods))
        return ChunkInsights(classes=classes, module_observations=[])

    def synthesize_overview(self, context_files, observations, metrics_json) -> ProjectOverview:
        telemetry.record("heuristic", cached=False)
        return ProjectOverview(
            purpose="(heuristic mode) See README excerpt in metadata; run with "
                    "ANTHROPIC_API_KEY for an LLM-grade overview.",
            functionality=[], architecture="", technology_stack=[],
            design_patterns=[], noteworthy_aspects=[],
        )


def make_extractor(cfg: AnalyzerConfig):
    cache = DiskCache(cfg.cache_path)
    if cfg.has_api_key:
        return ClaudeExtractor(cfg, cache)
    print("[warn] ANTHROPIC_API_KEY not set - falling back to heuristic extractor.")
    return HeuristicExtractor(cfg, cache)
