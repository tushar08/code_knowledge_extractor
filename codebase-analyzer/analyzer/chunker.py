"""Token-aware chunking.

Instead of sending raw files, we send compact *structural digests* produced by
the parser. Digests for classes in the same module are bin-packed into chunks
that respect the per-request token budget, so every LLM call stays well under
the model's context limit regardless of repository size.

Token budget guidance:
  MAX_TOKENS_PER_CHUNK (input)  = 8 000   (default)
  LLM_MAX_OUTPUT_TOKENS         = 8 192   (default)

  A chunk of 8K input tokens produces roughly 3-5K output tokens
  (one summary + one description per method × number of classes).
  The 8K/8K split leaves comfortable headroom on both sides.
"""
from typing import Dict, List

from .config import AnalyzerConfig
from .java_parser import ClassInfo


def class_digest(cls: ClassInfo) -> str:
    """Render a class as a compact text digest for the LLM.

    Omits:  imports, blank lines, method bodies, licence headers.
    Keeps:  name, role, annotations, extends/implements, injected deps,
            method signatures + HTTP mapping + cyclomatic complexity,
            javadoc (truncated).
    """
    lines = [
        f"FILE: {cls.file_path}",
        f"{cls.kind.upper()}: {cls.package}.{cls.name}  [role={cls.role}]",
    ]
    if cls.annotations:
        lines.append(f"  annotations: {', '.join('@' + a for a in cls.annotations)}")
    if cls.extends:
        lines.append(f"  extends: {cls.extends}")
    if cls.implements:
        lines.append(f"  implements: {', '.join(cls.implements)}")
    if cls.base_path:
        lines.append(f"  base path: {cls.base_path}")
    if cls.dependencies:
        lines.append(f"  injected deps: {', '.join(sorted(set(cls.dependencies)))}")
    if cls.javadoc:
        lines.append(f"  javadoc: {cls.javadoc[:200]}")   # tighter truncation
    for m in cls.methods:
        http = f" [{m.http_method} {m.http_path or ''}]" if m.http_method else ""
        lines.append(f"  - {m.signature}{http} (cc={m.cyclomatic_complexity})")
        if m.javadoc:
            lines.append(f"      doc: {m.javadoc[:120]}")  # tighter truncation
    return "\n".join(lines)


def _pack(digests: List[str], budget: int, cfg: AnalyzerConfig,
          module: str) -> List[dict]:
    """Greedy bin-pack a list of digests into budget-bounded chunks."""
    chunks: List[dict] = []
    current: List[str] = []
    current_tokens = 0
    for digest in digests:
        t = cfg.estimate_tokens(digest)
        # Single oversized digest: hard-truncate rather than silently skipping
        if t > budget:
            digest = digest[: int(budget * cfg.chars_per_token)]
            t = budget
        if current and current_tokens + t > budget:
            chunks.append({
                "module": module,
                "text": "\n\n".join(current),
                "tokens": current_tokens,
            })
            current, current_tokens = [], 0
        current.append(digest)
        current_tokens += t
    if current:
        chunks.append({
            "module": module,
            "text": "\n\n".join(current),
            "tokens": current_tokens,
        })
    return chunks


def build_chunks(classes_by_module: Dict[str, List[ClassInfo]],
                 cfg: AnalyzerConfig) -> List[dict]:
    """Greedy bin-packing of class digests into token-bounded chunks.

    Module boundaries are respected so each chunk is semantically coherent —
    the LLM always sees one logical slice of the codebase.
    """
    chunks: List[dict] = []
    for module, classes in classes_by_module.items():
        digests = [class_digest(cls) for cls in classes]
        chunks.extend(_pack(digests, cfg.max_tokens_per_chunk, cfg, module))
    return chunks


def split_chunk(chunk: dict, cfg: AnalyzerConfig) -> List[dict]:
    """Split a chunk in half (used as a retry strategy when output is truncated).

    Called by llm.py when the API returns stop_reason='max_tokens', which means
    the output JSON was truncated and Pydantic validation failed. Splitting the
    chunk halves the number of classes per request and therefore the expected
    output size.
    """
    texts = chunk["text"].split("\n\n")
    if len(texts) <= 1:
        # Can't split further — single class digest is itself too large.
        # Caller should truncate the digest or raise.
        return [chunk]
    mid = len(texts) // 2
    half_budget = cfg.max_tokens_per_chunk // 2
    halves = [
        _pack(texts[:mid], half_budget, cfg, chunk["module"]),
        _pack(texts[mid:], half_budget, cfg, chunk["module"]),
    ]
    return halves[0] + halves[1]
