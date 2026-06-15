"""Token-aware chunking.

Instead of sending raw files, we send compact *structural digests* produced by
the parser. Digests for classes in the same module are bin-packed into chunks
that respect the per-request token budget, so every LLM call stays well under
the model's context limit regardless of repository size.
"""
from typing import Dict, List

from .config import AnalyzerConfig
from .java_parser import ClassInfo


def class_digest(cls: ClassInfo, include_bodies_for: set = frozenset()) -> str:
    """Render a class as a compact text digest for the LLM."""
    lines = [f"FILE: {cls.file_path}",
             f"{cls.kind.upper()}: {cls.package}.{cls.name}  [role={cls.role}]"]
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
        lines.append(f"  javadoc: {cls.javadoc[:300]}")
    for m in cls.methods:
        http = f" [{m.http_method} {m.http_path or ''}]" if m.http_method else ""
        lines.append(f"  - {m.signature}{http} (cc={m.cyclomatic_complexity})")
        if m.javadoc:
            lines.append(f"      doc: {m.javadoc[:200]}")
    return "\n".join(lines)


def build_chunks(classes_by_module: Dict[str, List[ClassInfo]],
                 cfg: AnalyzerConfig) -> List[dict]:
    """Greedy bin-packing of class digests into token-bounded chunks.
    Module boundaries are respected where possible so each chunk is coherent."""
    chunks: List[dict] = []
    for module, classes in classes_by_module.items():
        current, current_tokens = [], 0
        for cls in classes:
            digest = class_digest(cls)
            t = cfg.estimate_tokens(digest)
            if t > cfg.max_tokens_per_chunk:           # single oversized class
                digest = digest[: int(cfg.max_tokens_per_chunk * cfg.chars_per_token)]
                t = cfg.max_tokens_per_chunk
            if current and current_tokens + t > cfg.max_tokens_per_chunk:
                chunks.append({"module": module, "text": "\n\n".join(current),
                               "tokens": current_tokens})
                current, current_tokens = [], 0
            current.append(digest)
            current_tokens += t
        if current:
            chunks.append({"module": module, "text": "\n\n".join(current),
                           "tokens": current_tokens})
    return chunks
