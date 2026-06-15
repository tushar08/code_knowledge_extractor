---
name: codebase-knowledge-extractor
description: >
  Build, run, and extend the Codebase Knowledge Extractor — a Python application
  that analyzes Java/Spring codebases using AST parsing, cyclomatic complexity
  metrics, and LLM-powered knowledge extraction (LangChain + Anthropic Claude),
  with a Streamlit web UI and LlamaIndex Q&A layer. Use this skill whenever the
  user wants to: create a code analysis tool, build a codebase-to-JSON knowledge
  pipeline, integrate LLMs with source code parsing, build a Streamlit dashboard
  for code insights, set up LlamaIndex retrieval over structured data, or extend
  any component of this application. Also trigger when the user mentions
  "codebase analyzer", "knowledge extractor", "code comprehension", "spring-rest-sakila",
  or asks about analyzing a Java project with AI.
---

# Codebase Knowledge Extractor — Build Harness

## What This Application Does

Reads a Java codebase → parses its structure via AST → measures complexity →
sends compact digests to Claude in token-safe batches → assembles a structured
JSON knowledge base → serves it through a Streamlit UI with Q&A.

```
read → parse → metrics → LLM map/reduce → JSON → Streamlit + Q&A
```

## Project Structure

```
codebase-analyzer/
├── .env.example          # All config (copy to .env)
├── .gitignore
├── requirements.txt
├── app.py                # Streamlit web UI (entry point)
│
├── analyzer/             # Core pipeline
│   ├── __init__.py
│   ├── config.py         # Settings from .env via python-dotenv
│   ├── code_reader.py    # Walks repo, loads .java files
│   ├── java_parser.py    # javalang AST → classes, methods, signatures, roles
│   ├── complexity.py     # Cyclomatic complexity, hotspots, endpoint inventory
│   ├── chunker.py        # Digest cards + token-budgeted bin packing
│   ├── schemas.py        # Pydantic models (ChunkInsights, ProjectOverview)
│   ├── llm.py            # LangChain Claude map/reduce + cache + heuristic fallback
│   └── main.py           # CLI orchestrator + progress callback
│
├── qa/                   # Q&A layer
│   ├── __init__.py
│   ├── engine.py         # knowledge.json → TextNodes → BM25/vector retrieval → answer
│   └── ask.py            # CLI for questions
│
├── output/
│   └── knowledge.json    # Structured output (generated)
│
├── insights/
│   └── claude_insights.json  # Pre-computed LLM insights (cache replay)
│
├── scripts/
│   └── apply_llm_insights.py # Merges cached insights into structural output
│
├── diagrams/             # Pipeline flow diagrams (SVG + PNG)
├── README.md
└── CODE_WALKTHROUGH.md
```

## Setup & Run

### Prerequisites

- Python 3.10+
- Git (for cloning repos)
- An Anthropic API key (optional — falls back to heuristic extractor without one)

### Installation

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY=sk-ant-api03-...
```

### Run modes

```bash
# CLI
python -m analyzer.main --repo ./spring-rest-sakila_tvr

# Streamlit web UI
streamlit run app.py

# Q&A (CLI)
python -m qa.ask -q "How does authentication work?" --mode bm25
python -m qa.ask --interactive
```

## Component Architecture

### 1. Config (`analyzer/config.py`)

All settings read from `.env` via `python-dotenv`. Key design:
- `_clean_api_key()` strips whitespace and filters placeholder values
- `has_api_key` property for conditional LLM usage
- `estimate_tokens()` — conservative heuristic (chars / 3.5)
- `load_dotenv(override=False)` so explicit env vars aren't clobbered

**When modifying:** add new settings as dataclass fields with `field(default_factory=lambda: _env("KEY", "default"))`.

### 2. Code Reader (`analyzer/code_reader.py`)

- `iter_source_files()` walks the repo, prunes excluded dirs in-place (`dirnames[:] = ...`), filters by extension, detects test files by path
- `read_context_files()` loads README + build files for LLM grounding
- Returns `SourceFile(path, content, loc, is_test)` dataclass

**When extending to new languages:** add extensions to `include_extensions` in config.

### 3. Java Parser (`analyzer/java_parser.py`)

Uses `javalang` to build an AST and extract:
- Classes/interfaces/enums with package, annotations, extends/implements
- Methods with full signatures, parameters, return types, modifiers
- **Role detection** from Spring annotations (`@RestController` → controller) with naming-convention fallback
- **REST mappings** (`@GetMapping` verb + path, class-level `@RequestMapping` base path)
- **Cyclomatic complexity** per method (AST decision nodes + `&&`/`||` regex)
- **Dependencies** from injected final fields / `@Autowired`

Key functions:
- `parse_java_file(src) → List[ClassInfo]` — the main entry point
- `_cyclomatic(method_node, source_slice) → int` — counts decision paths
- `_annotation_value(node, names) → str` — extracts annotation string values

**Graceful degradation:** files that fail to parse are skipped (returns empty list).

### 4. Complexity Metrics (`analyzer/complexity.py`)

Pure Python, zero LLM tokens. Produces:
- `totals` — files, classes, interfaces, enums, methods, LOC, endpoint count
- `class_roles` — Counter of roles
- `modules` — per-module class/method/LOC counts
- `cyclomatic_complexity` — avg, max, distribution (simple/moderate/complex), methods_over_10
- `complexity_hotspots` — top 15 most complex methods
- `rest_endpoints` — full verb+path+controller+handler inventory

### 5. Chunker (`analyzer/chunker.py`)

Two core ideas:
1. **Digest, don't dump.** `class_digest(cls)` renders a class as a compact text card (~4-5× smaller than raw source, zero semantic loss for comprehension).
2. **Bin packing.** `build_chunks(classes_by_module, cfg)` greedily packs digests into chunks under `max_tokens_per_chunk` (12K default), never mixing modules.

### 6. LLM Integration (`analyzer/llm.py`)

**Map/reduce with LangChain:**
- `ClaudeExtractor.analyze_chunk()` — map step: digest text → `ChunkInsights` (Pydantic)
- `ClaudeExtractor.synthesize_overview()` — reduce step: all observations → `ProjectOverview`
- `with_structured_output(PydanticModel)` forces schema-valid JSON via tool-calling

**Caching:** `DiskCache` keyed on SHA-256 content hash. Re-runs on unchanged code = zero API calls.

**Fallback:** `HeuristicExtractor` uses Spring naming conventions for free, offline descriptions.

**When changing models:** update `LLM_MODEL` in `.env`. Any LangChain ChatModel works — swap `ChatAnthropic` for `ChatOpenAI` etc.

### 7. Schemas (`analyzer/schemas.py`)

Pydantic models constraining LLM output:
- `MethodInsight(name, description)`
- `ClassInsight(qualified_name, summary, methods)`
- `ChunkInsights(classes, module_observations)` — map output
- `ProjectOverview(purpose, functionality, architecture, ...)` — reduce output

### 8. CLI Orchestrator (`analyzer/main.py`)

`run(repo, output, include_tests, progress_cb)` runs the 5-stage pipeline.
- `progress_cb(stage, detail, pct)` — optional callback for Streamlit progress bar
- Returns the complete knowledge dict

### 9. Q&A Engine (`qa/engine.py`)

- `load_nodes(knowledge_path)` — decomposes JSON into ~274 `TextNode`s at retrieval-friendly granularity: one per class, per controller endpoint list, per overview facet, per module observations, plus stats/hotspots
- `build_retriever(nodes, mode, top_k)` — vector (HuggingFace/OpenAI embeddings) with BM25 fallback
- `answer(question, retrieved, llm_model, api_key)` — Claude synthesis with explicit key passing; extractive fallback without a key

**Key design:** indexes extracted knowledge, not raw source → better recall per token.

### 10. Streamlit App (`app.py`)

Three-phase flow:
1. **Connect** — clone from URL (with auth error → PAT input flow) or use local path
2. **Configure** — sidebar: API key, model, token budget, options
3. **Results** — 7 tabs: Overview, Input, Modules, Endpoints, Complexity, Raw JSON, Q&A

**API key handling** (bug fix):
- `_clean_api_key()` in config.py filters placeholders so sidebar isn't pre-filled with dots
- `api_key` is stripped on input and passed explicitly to `answer(api_key=...)` 
- 401 errors get a specific user-facing message about key validity

## Common Modifications

### Add a new language parser
1. Create `analyzer/python_parser.py` (or `ts_parser.py`, etc.)
2. Return `List[ClassInfo]` with the same structure
3. Add the extension to `config.include_extensions`
4. The chunker, LLM, and output stages work unchanged

### Add a new Streamlit tab
1. Add to the `st.tabs([...])` list
2. Add a `with tab_name:` block
3. Read from `k = st.session_state["knowledge"]`

### Change the LLM provider
1. `pip install langchain-openai` (or any LangChain chat model)
2. In `llm.py`, swap `ChatAnthropic` for `ChatOpenAI` (or `ChatOllama` for local)
3. `with_structured_output()` works across all providers

### Add a new knowledge extraction field
1. Add field to `schemas.py` Pydantic models
2. The LLM will populate it automatically (schema-enforced)
3. Add rendering in `app.py` and assembly in `main.py`

### Deploy
```bash
# Docker
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501"]

# Or Streamlit Community Cloud: push to GitHub, connect at share.streamlit.io
```

## Key Design Decisions (for interviews)

| Decision | Why |
|---|---|
| Digest, don't dump | 4-5× token reduction, zero semantic loss for comprehension |
| Map/reduce over single prompt | Scales past context window, keeps each call focused |
| Pydantic + tool-calling | Valid JSON by construction, no parsing step |
| Module-coherent chunks | LLM sees one logical slice at a time → better summaries |
| Math to Python, meaning to LLM | Metrics are exact and free; LLM reserved for understanding |
| BM25 for code Q&A | Identifiers are exact-match friendly; zero keys, deterministic |
| Cache on content hash | Idempotent, re-runs cost zero tokens on unchanged code |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 401 "invalid x-api-key" | Placeholder key from .env being used | Clear `.env` placeholder or enter real key in sidebar |
| Clone hangs forever | Git waiting for username prompt | `GIT_TERMINAL_PROMPT=0` (already set in app.py) |
| "No .java files found" | Wrong repo path or non-Java project | Check path; extend `include_extensions` for other languages |
| javalang parse error | Newer Java syntax (records, sealed) | File is skipped gracefully; use tree-sitter for Java 17+ features |
| LLM returns empty insights | Token budget too small for large classes | Increase `MAX_TOKENS_PER_CHUNK` in `.env` |

## Dependencies

```
javalang          — Java AST parser
python-dotenv     — .env loading
pydantic          — Schema enforcement
langchain         — LLM orchestration
langchain-anthropic — Claude integration
llama-index-core  — Node/retriever infrastructure
llama-index-retrievers-bm25 — Lexical retrieval
llama-index-llms-anthropic  — Q&A synthesis (optional)
streamlit         — Web UI
cairosvg          — Diagram export (optional)
```
