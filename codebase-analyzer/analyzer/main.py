"""CLI entry point.

Usage:
    python -m analyzer.main --repo /path/to/spring-rest-sakila_tvr \
                            --output output/knowledge.json
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .chunker import build_chunks
from .code_reader import read_codebase, read_context_files
from .complexity import compute_metrics, module_of
from .config import AnalyzerConfig
from .java_parser import parse_java_file
from .llm import make_extractor


def run(repo: str, output: str, include_tests: bool = False,
        progress_cb=None) -> dict:
    """Run the full analysis pipeline.

    Args:
        progress_cb: optional callable(stage: str, detail: str, pct: float)
                     where pct is 0.0–1.0.  Used by the Streamlit UI.
    """
    def _p(stage, detail, pct=0.0):
        if progress_cb:
            progress_cb(stage, detail, pct)
        print(f"[{stage}] {detail}")

    cfg = AnalyzerConfig()
    cfg.include_tests = include_tests
    cfg.output_path = output

    # 1. Read ----------------------------------------------------------------
    _p("1/5", f"Reading codebase at {repo} ...", 0.05)
    files = read_codebase(repo, cfg)
    context_files = read_context_files(repo, cfg)
    _p("1/5", f"{len(files)} source files loaded.", 0.10)

    # 2. Parse ---------------------------------------------------------------
    _p("2/5", "Parsing Java sources ...", 0.15)
    classes = [c for f in files for c in parse_java_file(f)]
    _p("2/5", f"{len(classes)} types extracted.", 0.25)

    # 3. Metrics ---------------------------------------------------------------
    _p("3/5", "Computing complexity metrics ...", 0.30)
    metrics = compute_metrics(classes)
    _p("3/5", "Metrics computed.", 0.35)

    # 4. LLM extraction --------------------------------------------------------
    _p("4/5", "Running LLM knowledge extraction ...", 0.40)
    by_module = defaultdict(list)
    for c in classes:
        by_module[module_of(c)].append(c)
    chunks = build_chunks(by_module, cfg)
    _p("4/5", f"{len(chunks)} chunks (max {max((c['tokens'] for c in chunks), default=0)} "
              f"est. tokens/chunk, budget {cfg.max_tokens_per_chunk}).", 0.42)

    extractor = make_extractor(cfg)
    insight_by_class, observations = {}, []
    for i, chunk in enumerate(chunks, 1):
        pct = 0.42 + (i / len(chunks)) * 0.40
        _p("4/5", f"chunk {i}/{len(chunks)} [{chunk['module']}]", pct)
        res = extractor.analyze_chunk(chunk["module"], chunk["text"])
        for ci in res.classes:
            insight_by_class[ci.qualified_name] = ci
        observations.extend(f"[{chunk['module']}] {o}" for o in res.module_observations)

    overview = extractor.synthesize_overview(
        context_files, observations,
        json.dumps({k: metrics[k] for k in ("totals", "class_roles",
                                            "cyclomatic_complexity")}, indent=2))

    # 5. Assemble output -------------------------------------------------------
    _p("5/5", "Writing structured output ...", 0.88)
    modules_out = {}
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
                    "http_endpoint": (f"{m.http_method} "
                                      f"{(('/' + (c.base_path or '').strip('/')) if c.base_path else '')}"
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
            "llm": {"provider": "anthropic", "model": cfg.model_name,
                    "orchestration": "langchain", "mode": type(extractor).__name__},
            "token_strategy": {
                "max_tokens_per_chunk": cfg.max_tokens_per_chunk,
                "chunks_sent": len(chunks),
                "approach": "structural digests + module-coherent bin packing",
            },
        },
        "project_overview": overview.model_dump(),
        "statistics": metrics,
        "modules": modules_out,
    }

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(knowledge, indent=2))
    _p("5/5", f"Done -> {out_path} ({out_path.stat().st_size // 1024} KB)", 1.0)
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
    run(args.repo, args.output, args.include_tests)


if __name__ == "__main__":
    sys.exit(main())
