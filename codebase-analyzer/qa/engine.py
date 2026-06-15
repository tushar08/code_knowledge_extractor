"""Q&A over the extracted knowledge base (LlamaIndex).

Pipeline:
    knowledge.json -> TextNodes (class / module / overview / stats granularity)
                   -> retriever (vector embeddings OR BM25 fallback)
                   -> answer synthesis (Claude via LlamaIndex, or extractive)

Retrieval modes (auto-selected, override with --mode):
  - "vector": VectorStoreIndex with an embedding model. Uses a local
    HuggingFace model if available, else OpenAI embeddings if OPENAI_API_KEY
    is set. Best semantic recall.
  - "bm25":   lexical BM25 retriever (bm25s). Zero network, zero keys, fully
    deterministic - ideal for CI and air-gapped runs. Surprisingly strong on
    code Q&A because identifiers are exact-match friendly.

Answer modes:
  - If ANTHROPIC_API_KEY is set, retrieved nodes are synthesized into a
    natural-language answer by Claude (llama-index-llms-anthropic).
  - Otherwise the engine returns an extractive answer: the top-k retrieved
    knowledge nodes with scores - still genuinely useful for code Q&A.
"""
import json
import os
from pathlib import Path
from typing import List, Optional, Tuple

from llama_index.core.schema import NodeWithScore, TextNode

# --------------------------------------------------------------------------
# 1. knowledge.json -> nodes
# --------------------------------------------------------------------------

def _class_node(module: str, cls: dict) -> TextNode:
    lines = [f"{cls['kind'].title()} {cls['qualified_name']} (role: {cls['role']}, "
             f"module: {module}, file: {cls['file']})"]
    if cls.get("summary"):
        lines.append(f"Summary: {cls['summary']}")
    if cls.get("annotations"):
        lines.append("Annotations: " + ", ".join("@" + a for a in cls["annotations"]))
    if cls.get("extends"):
        lines.append(f"Extends: {cls['extends']}")
    if cls.get("implements"):
        lines.append("Implements: " + ", ".join(cls["implements"]))
    for m in cls.get("methods", []):
        ep = f" [{m['http_endpoint']}]" if m.get("http_endpoint") else ""
        desc = f" - {m['description']}" if m.get("description") else ""
        lines.append(f"Method: {m['signature']}{ep} "
                     f"(cyclomatic complexity {m['cyclomatic_complexity']}){desc}")
    return TextNode(
        text="\n".join(lines),
        id_=f"class::{cls['qualified_name']}",
        metadata={"type": "class", "module": module, "role": cls["role"],
                  "name": cls["name"], "file": cls["file"]},
    )


def load_nodes(knowledge_path: str) -> List[TextNode]:
    k = json.loads(Path(knowledge_path).read_text())
    nodes: List[TextNode] = []

    # Project overview - one node per facet for precise retrieval.
    ov = k.get("project_overview", {})
    for facet, value in ov.items():
        text = value if isinstance(value, str) else "\n".join(f"- {v}" for v in value)
        nodes.append(TextNode(text=f"Project {facet.replace('_', ' ')}:\n{text}",
                              id_=f"overview::{facet}",
                              metadata={"type": "overview", "facet": facet}))

    # Statistics: totals + complexity, hotspots, endpoint inventory per controller.
    stats = k.get("statistics", {})
    core = {x: stats.get(x) for x in ("totals", "class_roles", "cyclomatic_complexity")}
    nodes.append(TextNode(text="Codebase statistics:\n" + json.dumps(core, indent=1),
                          id_="stats::core", metadata={"type": "stats"}))
    if stats.get("complexity_hotspots"):
        hot = "\n".join(f"- cc={h['cyclomatic_complexity']} {h['class']}.{h['method']} "
                        f"({h['file']})" for h in stats["complexity_hotspots"])
        nodes.append(TextNode(text="Cyclomatic complexity hotspots (highest first):\n" + hot,
                              id_="stats::hotspots", metadata={"type": "stats"}))
    by_ctrl = {}
    for e in stats.get("rest_endpoints", []):
        by_ctrl.setdefault(e["controller"], []).append(e)
    for ctrl, eps in by_ctrl.items():
        text = f"REST endpoints handled by {ctrl}:\n" + "\n".join(
            f"- {e['http_method']} {e['path']} -> {e['handler']}()" for e in eps)
        nodes.append(TextNode(text=text, id_=f"endpoints::{ctrl}",
                              metadata={"type": "endpoints", "controller": ctrl}))

    # Modules: observations + one node per class.
    for module, mod in k.get("modules", {}).items():
        if mod.get("llm_observations"):
            nodes.append(TextNode(
                text=f"Observations for module {module}:\n" +
                     "\n".join(f"- {o}" for o in mod["llm_observations"]),
                id_=f"module::{module}", metadata={"type": "module", "module": module}))
        for cls in mod.get("classes", []):
            nodes.append(_class_node(module, cls))
    return nodes


# --------------------------------------------------------------------------
# 2. Retriever construction
# --------------------------------------------------------------------------

def _vector_retriever(nodes: List[TextNode], top_k: int):
    from llama_index.core import Settings, VectorStoreIndex
    embed = None
    try:  # local model first: no per-token cost, private
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        embed = HuggingFaceEmbedding(model_name="BAAI/bge-small-en-v1.5")
    except Exception:
        if os.environ.get("OPENAI_API_KEY"):
            from llama_index.embeddings.openai import OpenAIEmbedding
            embed = OpenAIEmbedding(model="text-embedding-3-small")
    if embed is None:
        raise RuntimeError("No embedding backend available")
    Settings.embed_model = embed
    return VectorStoreIndex(nodes).as_retriever(similarity_top_k=top_k)


def _bm25_retriever(nodes: List[TextNode], top_k: int):
    from llama_index.retrievers.bm25 import BM25Retriever
    return BM25Retriever.from_defaults(nodes=nodes, similarity_top_k=top_k)


def build_retriever(nodes: List[TextNode], mode: str = "auto", top_k: int = 5):
    if mode in ("auto", "vector"):
        try:
            return _vector_retriever(nodes, top_k), "vector"
        except Exception as e:
            if mode == "vector":
                raise
            print(f"[info] vector embeddings unavailable ({e.__class__.__name__}); "
                  f"using BM25 lexical retrieval.")
    return _bm25_retriever(nodes, top_k), "bm25"


# --------------------------------------------------------------------------
# 3. Answering
# --------------------------------------------------------------------------

SYNTH_PROMPT = (
    "You are a software architect answering questions about a codebase using "
    "ONLY the retrieved knowledge-base context below. Cite class names and "
    "files where relevant. If the context is insufficient, say so.\n\n"
    "Context:\n{context}\n\nQuestion: {question}\n\nAnswer:"
)


def answer(question: str, retrieved: List[NodeWithScore],
           llm_model: str = "claude-sonnet-4-6",
           api_key: str = "") -> Tuple[str, dict]:
    """Synthesize an answer from retrieved nodes.

    Returns:
        (answer_text, usage_dict) where usage_dict has input_tokens, output_tokens.
    """
    try:
        from analyzer.telemetry import telemetry
    except ImportError:
        telemetry = None

    context = "\n\n---\n\n".join(n.node.get_content() for n in retrieved)

    resolved_key = (api_key or os.environ.get("ANTHROPIC_API_KEY", "")).strip()
    if resolved_key in ("sk-ant-your-key-here", "your-key-here"):
        resolved_key = ""

    if resolved_key:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=resolved_key)
        prompt = SYNTH_PROMPT.format(context=context, question=question)
        response = client.messages.create(
            model=llm_model,
            max_tokens=1024,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        usage = {
            "input_tokens":  response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        if telemetry:
            telemetry.record("qa", input_tokens=usage["input_tokens"],
                             output_tokens=usage["output_tokens"], cached=False)
        return text, usage

    # Extractive fallback
    parts = ["[extractive mode — set ANTHROPIC_API_KEY for synthesized answers]\n"]
    for i, n in enumerate(retrieved, 1):
        score = f"{n.score:.3f}" if n.score is not None else "—"
        parts.append(f"### Source {i} (score {score}, "
                     f"{n.node.metadata.get('type')})\n{n.node.get_content()}")
    return "\n\n".join(parts), {"input_tokens": 0, "output_tokens": 0}
