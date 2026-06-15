"""CLI entry point.

Usage:
    python -m analyzer.main --repo /path/to/spring-rest-sakila_tvr \
                            --output output/knowledge.json

Design principle: the pipeline NEVER raises for LLM failures.
Every LLM call is wrapped so that:
  - A failed chunk falls back to the heuristic extractor for that chunk only
  - A failed overview falls back to an empty ProjectOverview
  - Warnings are collected and surfaced at the end (and via progress_cb)
  - Structural output (classes, endpoints, complexity) is always produced
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

from .chunker import build_chunks
from .code_reader import read_codebase, read_context_files
from .complexity import compute_metrics, module_of
from .config import AnalyzerConfig
from .java_parser import parse_java_file
from .llm import HeuristicExtractor, LLMUnavailableError, make_extractor
from .schemas import ChunkInsights, ProjectOverview


def _empty_overview() -> ProjectOverview:
    """Structural-only overview used when LLM is unavailable."""
    return ProjectOverview(
        purpose="LLM unavailable — structural analysis only. "
                "Set ANTHROPIC_API_KEY and re-run for AI-generated descriptions.",
        functionality=[],
        architecture="",
        technology_stack=[],
        design_patterns=[],
        noteworthy_aspects=[
            "LLM synthesis was skipped due to an API error. "
            "All structural data (classes, endpoints, complexity, dependencies) "
            "is complete and accurate."
        ],
    )


def run(repo: str, output: str, include_tests: bool = False,
        progress_cb=None) -> dict:
    """Run the full analysis pipeline.

    NEVER raises for LLM issues — always returns a complete knowledge dict.
    LLM warnings are collected in the returned dict under metadata.warnings.

    Args:
        progress_cb: optional callable(stage: str, detail: str, pct: float)
    """
    warnings: List[str] = []   # collected non-fatal LLM issues

    def _p(stage: str, detail: str, pct: float = 0.0, warn: bool = False):
        if warn:
            warnings.append(detail)
        if progress_cb:
            progress_cb(stage, detail, pct)
        prefix = "⚠️ " if warn else ""
        print(f"[{stage}] {prefix}{detail}")

    cfg = AnalyzerConfig()
    cfg.include_tests = include_tests
    cfg.output_path = output

    # ── 1. Read ───────────────────────────────────────────────────────────
    _p("1/5", f"Reading codebase at {repo} ...", 0.05)
    files = read_codebase(repo, cfg)
    context_files = read_context_files(repo, cfg)
    _p("1/5", f"{len(files)} source files loaded.", 0.10)

    # ── 2. Parse ──────────────────────────────────────────────────────────
    _p("2/5", "Parsing Java sources ...", 0.15)
    classes = [c for f in files for c in parse_java_file(f)]
    _p("2/5", f"{len(classes)} types extracted.", 0.25)

    # ── 3. Metrics ────────────────────────────────────────────────────────
    _p("3/5", "Computing complexity metrics ...", 0.30)
    metrics = compute_metrics(classes)
    _p("3/5", "Metrics computed.", 0.35)

    # ── 4. LLM extraction ─────────────────────────────────────────────────
    _p("4/5", "Running LLM knowledge extraction ...", 0.40)
    by_module = defaultdict(list)
    for c in classes:
        by_module[module_of(c)].append(c)
    chunks = build_chunks(by_module, cfg)
    max_chunk_tokens = max((c["tokens"] for c in chunks), default=0)
    _p("4/5",
       f"{len(chunks)} chunks (max {max_chunk_tokens} est. tokens/chunk, "
       f"budget {cfg.max_tokens_per_chunk}).", 0.42)

    # Pre-flight token ratio warning
    recommended_output = int(max_chunk_tokens * 0.6)
    if cfg.max_output_tokens < recommended_output:
        _p("4/5",
           f"LLM_MAX_OUTPUT_TOKENS={cfg.max_output_tokens} may be too small for "
           f"chunks of {max_chunk_tokens} tokens (recommend >={recommended_output}). "
           f"Will auto-retry oversized chunks by splitting.",
           0.43, warn=True)

    # Build extractor — falls back to heuristic automatically if no API key
    extractor = make_extractor(cfg)
    heuristic_fallback = HeuristicExtractor(cfg)

    insight_by_class: dict = {}
    observations: List[str] = []
    llm_mode = type(extractor).__name__
    chunks_ok = 0
    chunks_warn = 0

    for i, chunk in enumerate(chunks, 1):
        pct = 0.42 + (i / len(chunks)) * 0.40
        _p("4/5", f"chunk {i}/{len(chunks)} [{chunk['module']}]", pct)
        try:
            res: ChunkInsights = extractor.analyze_chunk(chunk["module"], chunk["text"])
            chunks_ok += 1
        except LLMUnavailableError as exc:
            # LLM failed for this chunk — fall back to heuristic for it only
            warn_msg = (
                f"LLM failed on chunk {i}/{len(chunks)} [{chunk['module']}] "
                f"— using heuristic fallback for this chunk. "
                f"Reason: {str(exc)[:120]}"
            )
            _p("4/5", warn_msg, pct, warn=True)
            chunks_warn += 1
            try:
                res = heuristic_fallback.analyze_chunk(chunk["module"], chunk["text"])
            except Exception:
                # Heuristic also failed (shouldn't happen) — produce empty insights
                res = ChunkInsights(classes=[], module_observations=[])
        except Exception as exc:
            # Unexpected error — log and continue
            warn_msg = (
                f"Unexpected error on chunk {i}/{len(chunks)} [{chunk['module']}]: "
                f"{type(exc).__name__}: {str(exc)[:120]}"
            )
            _p("4/5", warn_msg, pct, warn=True)
            chunks_warn += 1
            res = ChunkInsights(classes=[], module_observations=[])

        for ci in res.classes:
            insight_by_class[ci.qualified_name] = ci
        observations.extend(
            f"[{chunk['module']}] {o}" for o in res.module_observations
        )

    if chunks_warn:
        _p("4/5",
           f"{chunks_ok}/{len(chunks)} chunks used LLM; "
           f"{chunks_warn} fell back to heuristic.",
           0.82, warn=True)

    # Project overview (reduce step) — always produces output even on failure
    try:
        overview = extractor.synthesize_overview(
            context_files, observations,
            json.dumps(
                {k: metrics[k] for k in ("totals", "class_roles", "cyclomatic_complexity")},
                indent=2,
            ),
        )
    except LLMUnavailableError as exc:
        warn_msg = (
            f"LLM overview synthesis failed — structural-only output will be produced. "
            f"Reason: {str(exc)[:200]}"
        )
        _p("4/5", warn_msg, 0.85, warn=True)
        overview = _empty_overview()
    except Exception as exc:
        warn_msg = (
            f"Unexpected error in overview synthesis "
            f"({type(exc).__name__}: {str(exc)[:120]}) — using empty overview."
        )
        _p("4/5", warn_msg, 0.85, warn=True)
        overview = _empty_overview()

    # ── 5. Assemble output ────────────────────────────────────────────────
    _p("5/5", "Writing structured output ...", 0.88)
    modules_out: dict = {}
    for mod, mod_classes in sorted(by_module.items()):
        cls_out = []
        for c in sorted(mod_classes, key=lambda x: x.name):
            qn = f"{c.package}.{c.name}"
            ins = insight_by_class.get(qn)
            desc_by_method = {m.name: m.description for m in ins.methods} if ins else {}
            cls_out.append({
                "name": c.name, "qualified_name": qn, "kind": c.kind, "role": c.role,
                "file": c.file_path, "annotations": c.annotations,
                "extends": c.extends, "implements": c.implements,
                "summary": (ins.summary if ins else None) or c.javadoc,
                "methods": [{
                    "name": m.name,
                    "signature": m.signature,
                    "http_endpoint": (
                        f"{m.http_method} "
                        f"{('/' + (c.base_path or '').strip('/')) if c.base_path else ''}"
                        f"{('/' + m.http_path.strip('/')) if m.http_path else ''}"
                    ).strip() if m.http_method else None,
                    "cyclomatic_complexity": m.cyclomatic_complexity,
                    "description": desc_by_method.get(m.name) or m.javadoc,
                } for m in c.methods],
            })
        modules_out[mod] = {"class_count": len(cls_out), "classes": cls_out}

    knowledge = {
        "metadata": {
            "repository": repo,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "analyzer_version": "1.0.0",
            "llm": {
                "provider": "anthropic",
                "model": cfg.model_name,
                "orchestration": "langchain",
                "mode": llm_mode,
                "chunks_total": len(chunks),
                "chunks_llm": chunks_ok,
                "chunks_heuristic": chunks_warn,
            },
            "token_strategy": {
                "max_tokens_per_chunk": cfg.max_tokens_per_chunk,
                "max_output_tokens": cfg.max_output_tokens,
                "chunks_sent": len(chunks),
                "approach": "structural digests + module-coherent bin packing",
            },
            # All non-fatal warnings accumulated during the run
            "warnings": warnings,
        },
        "project_overview": overview.model_dump(),
        "statistics": metrics,
        "modules": modules_out,
    }

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(knowledge, indent=2))

    size_kb = out_path.stat().st_size // 1024
    if warnings:
        _p("5/5",
           f"Done with {len(warnings)} warning(s) → {out_path} ({size_kb} KB). "
           f"Structural data is complete; some LLM descriptions may be heuristic.",
           1.0)
    else:
        _p("5/5", f"Done → {out_path} ({size_kb} KB)", 1.0)

    return knowledge


def main():
    cfg = AnalyzerConfig()
    ap = argparse.ArgumentParser(description="LLM codebase knowledge extractor")
    ap.add_argument("--repo", default=cfg.repo_path or None,
                    required=not cfg.repo_path,
                    help="Path to the cloned repository (or set REPO_PATH in .env)")
    ap.add_argument("--output", default=cfg.output_path)
    ap.add_argument("--include-tests", action="store_true", default=cfg.include_tests)
    args = ap.parse_args()
    result = run(args.repo, args.output, args.include_tests)
    w = result.get("metadata", {}).get("warnings", [])
    if w:
        print(f"\n⚠️  {len(w)} warning(s) during run:")
        for msg in w:
            print(f"   • {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
