# Codebase Knowledge Extractor

> Reads a Java repository, extracts its structure via AST parsing, measures complexity, and uses Claude to generate a structured knowledge base — all served through a Streamlit web UI with RAG-powered Q&A.

---

## Overview

This tool answers a simple question: *what does this codebase actually do, and how complex is it?*

It does that in five steps — read files, parse Java structure, measure complexity, ask Claude to describe everything, and assemble one clean JSON output. A Streamlit app wraps the whole pipeline with a UI for cloning repos, running analysis, exploring results, and asking natural-language questions about the code.

**Analysed codebase:** [`spring-rest-sakila_tvr`](https://github.com/tushar08/spring-rest-sakila_tvr) — a Spring Boot 3 REST API over the MySQL Sakila DVD rental database.

**Results at a glance:**

| Metric | Value |
|---|---|
| Source files | 186 |
| Classes / interfaces / enums | 246 |
| Methods | 579 |
| Lines of code | 8,443 |
| REST endpoints | 70 |
| Avg cyclomatic complexity | 1.87 |
| LLM chunks sent | 11 |

---

## Quick Start

```bash
# 1. Clone this repo and the target codebase
git clone https://github.com/tushar08/spring-rest-sakila_tvr.git

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Open .env and set ANTHROPIC_API_KEY=sk-ant-...

# 4a. Run via CLI
python -m analyzer.main --repo ./spring-rest-sakila_tvr

# 4b. Or run the web UI (recommended)
streamlit run app.py
```

> **No API key?** The pipeline falls back to a free heuristic extractor — structural output (classes, signatures, endpoints, complexity) is still produced; only LLM-written summaries and descriptions are skipped.

---

## Web UI (`streamlit run app.py`)

The app has three phases:

**1. Connect a repository**
Paste any GitHub/GitLab/Bitbucket HTTPS URL and click **Clone**. The repo is cloned with `--depth 1`. If the repo is private and the clone fails, the 🔐 Authentication section auto-expands — enter a Personal Access Token and re-clone. The token is injected into the URL at runtime and never stored to disk. You can also point directly to a local path.

**2. Configure and run**
Set your API key, model, and token budget in the sidebar, then click **🚀 Run analysis**. A real-time progress bar tracks each stage and chunk.

**3. Explore results across 8 tabs**

| Tab | What you see |
|---|---|
| 📋 Overview | Project purpose, architecture, design patterns, noteworthy findings |
| 📂 Input | File tree, file sizes, and a live sample of the digest the LLM actually receives |
| 📦 Modules | Browse every module → class → method with summaries and complexity |
| 🌐 Endpoints | Full REST API inventory with coloured HTTP verb badges |
| 📊 Complexity | Distribution chart, average/max stats, refactoring hotspot table |
| 🗂️ Raw JSON | Download or browse any section of `knowledge.json` |
| 💬 Q&A | Ask questions in plain English, answered by Claude from the knowledge base |
| 📡 Telemetry | Per-call token usage, cost breakdown, cache savings, and optimisation tips |

---

## Project Structure

```
codebase-analyzer/
├── app.py                  # Streamlit web UI
├── .env.example            # All configuration keys with defaults
├── requirements.txt
│
├── analyzer/               # Core pipeline
│   ├── config.py           # Settings loaded from .env
│   ├── code_reader.py      # File walker and loader
│   ├── java_parser.py      # AST parser → classes, methods, roles, complexity
│   ├── complexity.py       # Metrics aggregation and hotspot detection
│   ├── chunker.py          # Digest cards + token-budgeted bin packing
│   ├── schemas.py          # Pydantic output schemas
│   ├── llm.py              # LangChain map/reduce + disk cache + fallback
│   ├── main.py             # CLI orchestrator with progress callback
│   └── telemetry.py        # Token and cost tracking
│
├── qa/
│   ├── engine.py           # LlamaIndex nodes + BM25/vector retrieval + answer synthesis
│   └── ask.py              # CLI for Q&A
│
├── output/
│   └── knowledge.json      # Generated knowledge base
│
├── insights/
│   └── claude_insights.json  # Pre-computed LLM insights (cache replay)
│
├── diagrams/               # Pipeline flow diagrams (SVG + PNG)
├── CODE_WALKTHROUGH.md     # Plain-English explanation of every file
└── SKILL.md                # Build harness for extending this project
```

---

## Configuration

All settings live in `.env` (copy from `.env.example`):

| Variable | Default | What it controls |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(empty)* | Your Anthropic key. Must start with `sk-ant-`. Without it, the heuristic extractor runs instead. |
| `LLM_MODEL` | `claude-sonnet-4-6` | Which Claude model to call. Switch to `claude-haiku-4-5` for ~5× lower cost. |
| `LLM_TEMPERATURE` | `0.0` | Randomness of LLM output. Keep at `0` for deterministic, reproducible results. |
| `LLM_MAX_OUTPUT_TOKENS` | `20000` | Maximum tokens Claude writes per response. Lowering risks truncated JSON schemas. |
| `MAX_TOKENS_PER_CHUNK` | `12000` | Token budget per LLM call. Lower = more calls, smaller each. Higher = fewer calls, more expensive. |
| `CHARS_PER_TOKEN` | `3.5` | Characters-per-token estimate. Deliberately conservative — Java source is denser than prose. |
| `REPO_PATH` | *(empty)* | Default repository path for the CLI. The web UI uses its own clone screen. |
| `INCLUDE_TESTS` | `false` | Whether to analyse test files. Doubles file count with limited architectural signal. |
| `OUTPUT_PATH` | `output/knowledge.json` | Where the final knowledge base is written. |
| `CACHE_PATH` | `output/llm_cache.json` | LLM response cache. Re-runs on unchanged code are free — zero API calls. |

---

## Output Structure

The pipeline produces a single hierarchical JSON file:

```jsonc
{
  "metadata": {
    "repository": "...",
    "analyzed_at": "2025-06-14T...",
    "llm": { "provider", "model", "mode" },
    "token_strategy": { "max_tokens_per_chunk", "chunks_sent", "approach" }
  },
  "project_overview": {
    "purpose": "...",
    "functionality": [ "..." ],
    "architecture": "...",
    "technology_stack": [ "..." ],
    "design_patterns": [ "..." ],
    "noteworthy_aspects": [ "..." ]
  },
  "statistics": {
    "totals": { "files", "classes", "methods", "lines_of_code", "rest_endpoints" },
    "cyclomatic_complexity": { "average", "max", "distribution", "methods_over_10" },
    "complexity_hotspots": [ { "class", "method", "cyclomatic_complexity", "file" } ],
    "rest_endpoints": [ { "http_method", "path", "controller", "handler" } ]
  },
  "modules": {
    "services.catalog": {
      "llm_observations": [ "..." ],
      "classes": [{
        "name", "qualified_name", "kind", "role", "summary",
        "methods": [{ "name", "signature", "http_endpoint", "cyclomatic_complexity", "description" }]
      }]
    }
  }
}
```

---

## Key Findings from the Analysed Codebase

- **Architecture:** Modular monolith with 8 vertical service slices (auth, catalog, customer, location, payment, rental, staff, store) over shared `common` and `config` kernels. Each slice owns its own controller → service → repository → entity stack.
- **Security:** 70 REST endpoints, every handler annotated with `@Secured`. JWT authentication via a filter + provider pair with `ROLE_READ` / `ROLE_MANAGE` roles.
- **Complexity:** Average cyclomatic complexity of 1.87. All 12 methods with CC > 10 are hand-rolled `equals()` implementations on large DTOs (max CC = 16 on `FilmEntity`). One `@EqualsAndHashCode` from Lombok would eliminate the entire hotspot list.
- **Tooling:** Heavy compile-time tooling — MapStruct for DTO mapping, Querydsl for type-safe queries, Lombok for boilerplate reduction. API docs generated from tests via Spring REST Docs.

---

## How It Works

### The pipeline

```
read files  →  parse (AST)  →  measure complexity  →  LLM map/reduce  →  assemble JSON
code_reader    java_parser      complexity              llm + chunker      main
```

### Stage 1 — Read (`code_reader.py`)

Walks the repository with `os.walk`, pruning excluded directories (`.git`, `build`, `target`, `gradle`) in-place for efficiency. Filters to `.java` production sources; detects test files by path and excludes them by default. Also loads `README.md` and Gradle/Maven build files separately as grounding context for the LLM's project overview.

### Stage 2 — Parse (`java_parser.py`)

Uses `javalang` to build an AST and extract structured facts:

- **Identity** — package, class/interface/enum name, kind
- **Role** — derived from Spring annotations (`@RestController` → controller, `@Service` → service) with naming-convention fallback (`*Repository`, `*Mapper`, `*Converter`, etc.)
- **Methods** — full signature (modifiers, return type, parameters), annotations, javadoc
- **REST mappings** — HTTP verb and path from `@GetMapping`, `@PostMapping` etc., plus class-level `@RequestMapping` base paths
- **Dependencies** — injected fields (`final` or `@Autowired`), showing collaboration structure
- **Cyclomatic complexity** — counted per method from AST decision nodes (`if`, `for`, `while`, `switch` cases, `catch`, ternary) plus `&&` / `||` operators

Files that fail to parse are skipped gracefully rather than crashing the run.

### Stage 3 — Measure (`complexity.py`)

Pure Python, zero LLM tokens. Aggregates:

- Project totals (files, classes, interfaces, enums, methods, LOC, endpoint count)
- Class role distribution
- Per-module statistics (class count, method count, LOC)
- Complexity distribution bucketed into simple (CC 1–4), moderate (5–10), and complex (>10)
- Top 15 complexity hotspots — instant refactoring candidates
- Full REST endpoint inventory (verb + path + controller + handler)

### Stage 4 — LLM extraction (`chunker.py`, `llm.py`, `schemas.py`)

**The token problem and how we solve it:**

Sending raw source files would cost ~4–5× more tokens for the same information. Instead:

1. **Digest cards** — `class_digest()` renders each class as a compact text card: name, role, annotations, injected dependencies, and one line per method signature with complexity. All signal, no boilerplate.
2. **Bin-packing** — cards are greedily packed into chunks under the `MAX_TOKENS_PER_CHUNK` budget (12K default). Chunks never mix modules — the LLM always sees a self-consistent slice of the codebase.
3. **Map step** — each chunk → one Claude call → `ChunkInsights` (per-class summaries, per-method descriptions, design observations). Chunks are independent, so this step could be parallelised.
4. **Reduce step** — all per-module observations + README/build context + aggregated metrics → one final Claude call → `ProjectOverview` (purpose, functionality, architecture, stack, patterns, noteworthy aspects).

For this repo: 186 files → 11 chunks → max 8.3K tokens per chunk.

**Structured output:** LangChain's `with_structured_output(PydanticModel)` uses Anthropic tool-calling to enforce the response schema. Output is valid JSON by construction — there is no free-text parsing step.

**Caching:** every LLM call is cached on a SHA-256 hash of its input (`model_name + content`). Re-runs on unchanged code cost zero tokens. Changing `LLM_MODEL` automatically invalidates all cache entries.

### Stage 5 — Assemble (`main.py`)

Merges static facts from the parser with LLM insights into the final JSON. Accepts an optional `progress_cb(stage, detail, pct)` callback used by the Streamlit UI for real-time progress updates.

---

## RAG-Powered Q&A (`qa/`)

The Q&A layer is a full RAG (Retrieve → Generate) pipeline over the extracted knowledge base.

**CLI:**
```bash
python -m qa.ask -q "How does authentication work?"
python -m qa.ask --interactive
```

**How it works:**

1. **Index** — `knowledge.json` is decomposed into ~274 `TextNode`s at retrieval-friendly granularity: one per class (summary + signatures + endpoints + complexity), per controller's endpoint list, per overview facet, per module's observations, plus statistics and hotspot nodes. Each node carries metadata (`type`, `module`, `role`, `file`) for filtering and citation.

2. **Retrieve** — BM25 lexical retrieval by default (deterministic, zero keys, strong on code Q&A because class and method names are exact-match friendly). Vector retrieval is also supported via local HuggingFace embeddings (`bge-small-en-v1.5`) or OpenAI embeddings — switch with `--mode vector`.

3. **Generate** — with an API key, the top-k retrieved nodes are passed to Claude with a prompt that restricts answers to the retrieved context (preventing hallucination). Without a key, the best-matching node is returned directly as an extractive answer.

**Why index extracted knowledge instead of raw source?** Retrieval operates over dense, curated text rather than noisy code with imports and boilerplate. Answers automatically inherit the analysis layer — roles, complexity, architectural observations — without any extra work.

---

## Token Telemetry

Every LLM call is recorded in `output/telemetry.json` and surfaced in the **📡 Telemetry** tab:

- Input/output tokens and estimated cost per call
- Breakdown by call type (map, reduce, Q&A, heuristic)
- Cache hit rate and estimated tokens saved
- Per-call event log with timestamps
- Optimisation tips keyed to your actual usage pattern

Cost reference (claude-sonnet-4): $3/M input · $15/M output.

---

## Limitations

| Area | Current behaviour | Upgrade path |
|---|---|---|
| Java version | `javalang` covers Java 8–11; newer syntax (records, sealed types) causes files to be silently skipped | Replace parser with `tree-sitter-java` for Java 17+ |
| Token estimation | Conservative heuristic (chars / 3.5) | Plug in Anthropic's count-tokens API in `AnalyzerConfig.estimate_tokens` |
| Complexity measurement | Counts `&&`/`||` textually rather than via full control-flow graph | Accurate enough in practice; tree-sitter CFG for exactness |
| Rate-limit handling | Pipeline fails mid-run on 429; cache preserves completed work | Add `llm.with_retry(stop_after_attempt=3)` in `llm.py` |
| Language support | Java only | Add per-language parsers that emit `ClassInfo` objects; the rest of the pipeline is language-agnostic |
| Hallucination detection | No post-hoc verifier on LLM descriptions | Cross-check method names in `ChunkInsights` against parsed class inventory |

---

## Possible Extensions

- **Call-graph extraction** — enrich method descriptions with caller/callee relationships
- **Incremental analysis** — `git diff` to re-analyse only changed files against the cache
- **Multi-language** — tree-sitter parsers for Python, TypeScript, Go; same pipeline, language-swappable parser layer
- **Microservices fleet** — produce one `knowledge.json` per service plus a cross-service dependency graph and fleet-level overview
- **Test coverage integration** — overlay JaCoCo/coverage data onto the complexity hotspot map

---

## Deliverables

| File | Description |
|---|---|
| `analyzer/` | Core analysis pipeline (5 stages) |
| `qa/` | RAG Q&A engine (LlamaIndex + Claude) |
| `app.py` | Streamlit web UI |
| `output/knowledge.json` | Generated knowledge base for the Sakila codebase |
| `README.md` | This document |
| `CODE_WALKTHROUGH.md` | Plain-English file-by-file explanation with interview Q&A |
| `SKILL.md` | Build harness for extending or recreating this project |
| `diagrams/` | Pipeline flow diagrams (SVG + PNG) |
