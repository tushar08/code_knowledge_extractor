# Codebase Knowledge Extractor

A program that analyzes a Java/Spring codebase, feeds it to an LLM within strict token limits, and emits a structured, machine-readable JSON knowledge base.

**Target codebase:** [tushar08/spring-rest-sakila_tvr](https://github.com/tushar08/spring-rest-sakila_tvr) — a Spring Boot 3 REST API over the MySQL Sakila sample database (186 source files, 246 types, 579 methods, 70 REST endpoints).

**LLM:** Anthropic Claude (`claude-sonnet-4`), orchestrated through **LangChain** with Pydantic-enforced structured output.

## Deliverables

| Deliverable | Location |
|---|---|
| Source code | `analyzer/` (reader, parser, chunker, complexity, LLM integration, CLI) |
| Streamlit web UI | `app.py` |
| Q&A engine | `qa/` |
| Structured output | `output/knowledge.json` |
| Documentation | this README + `CODE_WALKTHROUGH.md` |

## Quick start

```bash
git clone https://github.com/tushar08/spring-rest-sakila_tvr.git
pip install -r requirements.txt

# Configure (copy and edit)
cp .env.example .env           # then set ANTHROPIC_API_KEY inside

# Option A: CLI
python -m analyzer.main --repo ./spring-rest-sakila_tvr

# Option B: Streamlit web UI
streamlit run app.py
```

Without an API key the pipeline degrades gracefully to a deterministic heuristic extractor (structural output only, convention-based descriptions).

## Configuration (`.env`)

All settings live in a single `.env` file (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Your Anthropic API key |
| `LLM_MODEL` | `claude-sonnet-4-20250514` | Claude model to use |
| `LLM_TEMPERATURE` | `0.0` | LLM temperature (0 = deterministic) |
| `LLM_MAX_OUTPUT_TOKENS` | `4096` | Max output tokens per LLM call |
| `MAX_TOKENS_PER_CHUNK` | `12000` | Token budget per chunk |
| `CHARS_PER_TOKEN` | `3.5` | Character-to-token ratio (conservative) |
| `REPO_PATH` | — | Default repo path (CLI uses `--repo` to override) |
| `INCLUDE_TESTS` | `false` | Analyze test files |
| `OUTPUT_PATH` | `output/knowledge.json` | Where to write the output |
| `CACHE_PATH` | `output/llm_cache.json` | LLM response cache |

## Streamlit web UI

```bash
streamlit run app.py
```

### Repository setup (first screen)

The app opens with a **Connect a repository** screen before anything else:

1. **Clone from URL** (default) — paste any GitHub/GitLab/Bitbucket HTTPS URL and click **Clone**. The repo is cloned with `--depth 1` into `./repos/`.
2. **Private repos** — if the clone fails with an auth error (403, 401, "could not read Username"), the 🔐 Authentication section auto-expands with instructions for generating a Personal Access Token (GitHub, GitLab, or Bitbucket). Enter the PAT and re-clone — the token is injected into the HTTPS URL at runtime, never stored to disk.
3. **Use local path** — point to an already-cloned directory. The app validates it's a git repo with `.java` files before proceeding.
4. **Re-clone** — if the repo was cloned before, a **Re-clone** button lets you delete and fetch fresh.

Once the repo is connected, the sidebar shows LLM/token/output settings and the **Run analysis** button.

### Result tabs

| Tab | What it shows |
|---|---|
| **Overview** | Project purpose, architecture, stats, design patterns, noteworthy findings |
| **Input** | File tree, file-size table, and a live sample of the "digest card" the LLM actually sees |
| **Modules** | Browse every module → class → method with summaries, endpoints, and complexity scores |
| **Endpoints** | Full REST API inventory grouped by controller, with colored HTTP verb badges |
| **Complexity** | Distribution chart, average/max stats, and the hotspot table of refactoring candidates |
| **Raw JSON** | Download or browse any section of `knowledge.json` |
| **Q&A** | Ask questions in English — BM25 retrieval + Claude synthesis (or extractive fallback) |

All sidebar settings (API key, model, token budget) can be changed at runtime without editing `.env`. Click **↩ Change repository** in the sidebar to return to the clone screen.

## Approach

The pipeline has five stages:

```
read  ->  parse (AST)  ->  metrics  ->  LLM map/reduce  ->  assemble JSON
```

**1. Read (`code_reader.py`).** Walks the repository, prunes build/VCS directories in-place for efficiency, filters to `.java` production sources (tests optional via `--include-tests`), and separately loads grounding context (README, Gradle build files).

**2. Parse (`java_parser.py`).** Uses the `javalang` AST parser to extract packages, classes/interfaces/enums, method signatures (modifiers, return types, parameters), annotations, javadoc, injected dependencies, and REST mapping metadata (`@GetMapping` paths, class-level base paths). Each class is also assigned an architectural **role** (controller, service, repository, entity, …) from its Spring annotations and naming conventions.

**3. Metrics (`complexity.py`).** Computes per-method **cyclomatic complexity** from AST decision points (if/for/while/switch/catch/ternary plus `&&`/`||`), then aggregates project totals, per-module statistics, a complexity distribution, the top hotspots, and the full REST endpoint inventory — all computed statically, for free, without spending a single LLM token.

**4. LLM extraction (`chunker.py`, `llm.py`, `schemas.py`).** A **map/reduce** design:
- *Map:* compact **structural digests** of classes (not raw source) are bin-packed into module-coherent chunks under a 12K-token budget; each chunk yields per-class summaries, per-method descriptions, and design observations.
- *Reduce:* the README/build context, aggregated metrics, and all per-module observations are synthesized into the project overview (purpose, functionality, architecture, stack, patterns, noteworthy aspects).

**5. Assemble (`main.py`).** Static facts and LLM insights are merged into a single hierarchical JSON: `metadata → project_overview → statistics → modules → classes → methods`.

## Token-limit strategy (key design decision)

Sending raw files does not scale and wastes tokens on imports, getters, and boilerplate. Instead:

1. **Digest, don't dump.** The AST pass reduces ~8,400 LOC to compact structural digests (signatures + annotations + javadoc + complexity). For this repo that's an ~4–5× token reduction with near-zero semantic loss for comprehension tasks.
2. **Budgeted bin-packing.** Digests are greedily packed into chunks capped at 12K estimated tokens (conservative ~3.5 chars/token heuristic), keeping every request far below the model's context window. This repo fits in **11 chunks, max ~8.3K tokens each**.
3. **Module coherence.** Chunks never mix modules, so the LLM always sees a self-consistent slice (e.g. all of `services.rental`) — better summaries, no cross-module confusion.
4. **Caching.** Every LLM call is cached on a SHA-256 content hash (`output/llm_cache.json`); re-runs on unchanged code cost zero tokens and are fully idempotent.

## Machine-readable output (best practice)

- LLM responses are constrained with LangChain's `with_structured_output(PydanticModel)` (Anthropic tool-calling under the hood) — output is **schema-validated JSON by construction**, never free-text parsing.
- `temperature=0` for deterministic, reproducible extraction.
- The final JSON has a stable, documented shape; every method carries `name`, `signature`, optional `http_endpoint`, `cyclomatic_complexity`, and `description`.

## Output structure

```jsonc
{
  "metadata":         { "repository", "llm", "token_strategy", ... },
  "project_overview": { "purpose", "functionality", "architecture",
                        "technology_stack", "design_patterns", "noteworthy_aspects" },
  "statistics":       { "totals", "class_roles", "modules",
                        "cyclomatic_complexity", "complexity_hotspots", "rest_endpoints" },
  "modules": {
    "services.catalog": {
      "llm_observations": [ ... ],
      "classes": [{
        "name", "qualified_name", "kind", "role", "annotations", "summary",
        "methods": [{ "name", "signature", "http_endpoint",
                      "cyclomatic_complexity", "description" }]
      }]
    }
  }
}
```

## Selected findings (from `output/knowledge.json`)

- Layered **modular monolith** with 8 vertically sliced services (auth, catalog, customer, location, payment, rental, staff, store) over shared `common`/`config` kernels.
- **70 secured REST endpoints**; every handler carries `@Secured` with READ/MANAGE roles; JWT auth implemented as a filter + provider pair.
- Very low complexity overall (avg CC **1.87**); all 12 methods with CC > 10 are hand-rolled `equals()` on large DTOs/entities (max CC 16) — the main refactoring candidate.
- Heavy compile-time tooling: MapStruct, Querydsl APT, Lombok; test-driven API docs via Spring REST Docs.

## Assumptions & limitations

- **Java-focused parsing.** `javalang` targets Java; Kotlin/Gradle build logic is used only as overview context. `javalang` predates some newer Java syntax (records, sealed types); unparseable files are skipped gracefully (none in this repo).
- **Token estimation is heuristic** (chars/3.5, deliberately conservative). A model-exact tokenizer or Anthropic's count-tokens API can be plugged into `AnalyzerConfig.estimate_tokens`.
- **Cyclomatic complexity** counts boolean operators textually within the method slice — accurate in practice, but an approximation versus a full control-flow graph.
- Tests are excluded by default (`--include-tests` to add them).
- The bundled `output/knowledge.json` was produced by this pipeline with the LLM stage performed by **Claude**; the insights are checked in at `insights/claude_insights.json` and merged via `scripts/apply_llm_insights.py` (the same mechanism as the LLM cache), so the deliverable can be reproduced without an API key. Running live with `ANTHROPIC_API_KEY` regenerates them end-to-end.

## Q&A over the knowledge base (LlamaIndex)

The `qa/` package turns `knowledge.json` into a queryable index:

```bash
python -m qa.ask -q "How does authentication work in this codebase?"
python -m qa.ask --interactive
```

**How it works (`qa/engine.py`):**

1. **Node construction.** The JSON is decomposed into ~274 LlamaIndex `TextNode`s at retrieval-friendly granularity: one node per class (summary + signatures + endpoints + complexity), per module observation set, per overview facet (purpose, architecture, …), per controller endpoint inventory, plus statistics and hotspot nodes. Metadata (`type`, `module`, `role`, `file`) is attached for filtering and citation.
2. **Pluggable retrieval.** `--mode vector` builds a `VectorStoreIndex` with embeddings (local HuggingFace `bge-small-en-v1.5` preferred — free and private; OpenAI `text-embedding-3-small` if `OPENAI_API_KEY` is set). `--mode bm25` uses a lexical BM25 retriever (`bm25s`) — zero keys, zero network, deterministic, and strong on code Q&A since identifiers like `RentalController` are exact-match friendly. `--mode auto` (default) tries vector, then falls back to BM25.
3. **Answer synthesis.** With `ANTHROPIC_API_KEY` set, the top-k retrieved nodes are synthesized into a grounded natural-language answer by Claude through `llama-index-llms-anthropic` (the prompt forbids answering beyond the retrieved context). Without a key, the engine returns an **extractive answer**: the ranked evidence nodes with scores — still directly usable.

Example (BM25, extractive): *"Which REST endpoints exist for rentals?"* retrieves the `RentalController` endpoint inventory as the top hit (GET/POST `/rentals`, PUT `/rentals/return`, …); *"highest complexity hotspots?"* surfaces the catalog-module observation and the noteworthy-aspects node identifying the `equals()` cc-16 hotspots.

Design note: indexing the *extracted knowledge* rather than raw source means retrieval operates over dense, LLM-curated text — better recall per token than chunked source files, and answers automatically inherit the analysis layer (roles, complexity, observations).

## Possible extensions

- Call-graph extraction to enrich method descriptions with caller/callee context.
- Multi-language support via tree-sitter parsers.
- Incremental analysis driven by `git diff` against the cache.
