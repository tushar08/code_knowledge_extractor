# Code Walkthrough — Explained in Simple Terms

A plain-language guide to how the Codebase Knowledge Extractor works, file by file. Written so you can explain any part of it in an interview without reading the code line by line.

---

## The Big Picture (30-second version)

> "My program reads a Java codebase, uses a parser to pull out the *structure* (classes, methods, signatures), measures complexity mathematically, and then sends compact summaries — not raw code — to Claude in small, token-safe batches. Claude describes what each class and method does, and a final call synthesizes a project overview. Everything is merged into one structured JSON file. On top of that, I built a Q&A layer with LlamaIndex so you can ask the JSON questions in plain English."

The pipeline, as a kitchen analogy: **read** the groceries in, **chop** them into ingredients (parsing), **weigh** them (metrics), have the **chef** taste each dish in small portions (LLM map step), have the chef write the **menu description** (LLM reduce step), and **plate** it all as one JSON (assembly).

```
 read files → parse structure → measure complexity → LLM analysis → final JSON → (Q&A)
 code_reader   java_parser       complexity           llm + chunker   main         qa/
```

---

## Visual Overview (flow diagrams)

Three diagrams that tell the whole story — also available as standalone images in `diagrams/` (SVG + PNG) for slides.

**1. The full pipeline.** Steps 1–3 are plain Python (fast, free, exact); only step 4 uses AI.

![Pipeline overview](diagrams/1_pipeline_overview.png)

**2. How token limits are never exceeded.** Each class is shrunk into a small "digest card" (name, role, method signatures, complexity — no boilerplate), and cards are packed into batches that always stay under the 12,000-token budget. 186 files became just 11 batches.

![Digest and chunking](diagrams/2_digest_and_chunking.png)

**3. Claude works in two rounds (map/reduce).** Round one: describe every class, one batch at a time. Round two: read the README plus all round-one findings and write the big-picture overview. The final JSON merges the parser's hard facts with Claude's explanations.

![Claude map/reduce](diagrams/3_claude_map_reduce.png)

**One analogy that covers everything:** it's a book report on a 1,000-page book. You don't hand the reviewer the whole book — you write an index card per chapter (parsing), note how hard each chapter is (complexity), hand over a few cards at a time (chunking), collect the chapter notes (map), then ask for the overall review (reduce). The book report is `knowledge.json`.

---

## File-by-File Logic

### 1. `analyzer/config.py` — the settings dial

One dataclass holding every tunable number in one place: which LLM model, the token budget per request (12,000), which file types to read, which folders to skip (`.git`, `build`), and where output goes.

**The one clever bit:** `estimate_tokens()` — a cheap token estimator that assumes ~3.5 characters per token. It's deliberately *conservative* (overestimates), so chunks always land safely under the limit. If asked why not an exact tokenizer: "Exact counting needs the model's tokenizer; my heuristic is free, offline, and erring on the safe side costs nothing. The method is one line — swapping in an exact counter later is trivial."

### 2. `analyzer/code_reader.py` — the file collector

Walks the repository folder by folder and loads source files.

Logic in plain terms:
- Skip junk folders (`.git`, `build`, `gradle`) — and skip them *before* descending into them (`dirnames[:] = ...` prunes the walk in-place, so we never even enter them — efficiency).
- Keep only `.java` files; detect test files by path (`src/test/...`) and exclude them by default.
- Count non-blank lines per file (LOC).
- Separately load the README and Gradle build files — these become *context* the LLM later uses to write the project overview.

Returns a simple list of `SourceFile` objects: path, content, line count, is-it-a-test flag.

### 3. `analyzer/java_parser.py` — the structure extractor (the heart)

This is where raw text becomes structured facts, using `javalang`, a real Java parser that builds an **AST** (Abstract Syntax Tree — the code as a tree of grammar elements, the same thing a compiler builds, instead of flat text).

For every class it extracts:
- **Identity:** package, name, kind (class / interface / enum)
- **Role:** is it a controller? a service? a repository? Determined by Spring annotations (`@RestController` → controller, `@Service` → service) with a naming-convention fallback (`*Repository` → repository, `*Mapper` → mapper). This works because **Spring code is self-describing** — the annotations literally announce each class's job.
- **Methods:** name, full signature (modifiers + return type + parameters), annotations, javadoc.
- **REST info:** reads `@GetMapping("/films")` etc. so we know each handler's HTTP verb and URL.
- **Dependencies:** the injected fields (final fields / `@Autowired`), showing what each class collaborates with.

**Cyclomatic complexity** — explain it as: *"the number of independent paths through a method — roughly, 1 + the number of decisions."* The code walks the method's AST counting decision nodes (`if`, `for`, `while`, `switch` cases, `catch`, ternaries) and adds the `&&`/`||` operators found in the method's text. A method with cc=1 is a straight line; cc=16 has many branching paths and is hard to test.

Defensive detail worth mentioning: files that fail to parse are skipped gracefully instead of crashing the run.

### 4. `analyzer/complexity.py` — the statistician

Takes all parsed classes and aggregates the numbers:
- Project totals (files, classes, methods, LOC, endpoint count)
- Class counts per role and per module (module = the service slice, derived from the package name, e.g. `...services.catalog.controller` → `services.catalog`)
- Complexity stats: average, max, and a distribution bucketed into simple (1–4) / moderate (5–10) / complex (>10)
- **Hotspots:** the top 15 most complex methods — instant refactoring targets
- The full REST endpoint inventory (verb + path + controller + handler)

**Key talking point:** all of this is computed *statically*, by code, for **zero LLM tokens**. The LLM is reserved for what only an LLM can do — understanding and describing. Math goes to Python; meaning goes to the model.

### 5. `analyzer/chunker.py` — the token-budget packer

The core answer to "how do you stay under token limits?" Two ideas:

**Idea 1 — Digest, don't dump.** We never send raw source. `class_digest()` renders each class as a compact text card: name, role, annotations, dependencies, and one line per method signature with its complexity. All the meaning, none of the boilerplate (no imports, no braces, no getter bodies). Roughly a 4–5× token reduction.

**Idea 2 — Bin-packing with a budget.** Digests are greedily packed into chunks: keep adding digests until the next one would blow the 12K-token budget, then start a new chunk. Crucially, **chunks never mix modules** — the LLM always sees one coherent slice (all of `services.rental` together), which yields better summaries and no cross-module confusion. Oversized single classes get truncated to the budget rather than crashing.

Result on this repo: 186 files → just **11 chunks**, largest ~8.3K tokens.

### 6. `analyzer/schemas.py` — the output contract

Pydantic models defining *exactly* what the LLM must return: `ChunkInsights` (per-class summaries + per-method one-liners + observations) for the map step, and `ProjectOverview` (purpose, functionality, architecture, stack, patterns, noteworthy aspects) for the reduce step.

**Why this matters:** LangChain's `with_structured_output(PydanticModel)` makes the LLM respond via tool-calling that's validated against the schema. So the output is **valid JSON by construction** — there is no "parse the model's free text and hope" step anywhere. This is the direct answer to the challenge's "consistent and machine-readable" requirement.

### 7. `analyzer/llm.py` — the LLM brain

Implements a **map/reduce** pattern (borrowed from big-data processing):

- **Map:** each chunk → one LLM call → structured insights for those classes. Chunks are independent, so no call ever needs the whole codebase in context — this is what makes the approach scale to repos of any size.
- **Reduce:** one final call takes the README + build files + aggregated metrics + all observations from the map step, and synthesizes the high-level project overview. The model writes the overview *grounded in evidence* gathered bottom-up, not from guesswork.

Three supporting decisions:
- **Caching:** every call is cached on a SHA-256 hash of its input. Re-run on unchanged code → zero API calls, zero cost, identical output (idempotent).
- **Temperature 0:** deterministic, reproducible extraction — right for analysis, wrong only for creative writing.
- **Graceful degradation:** no API key? A `HeuristicExtractor` takes over, generating convention-based descriptions (`getFilmList` → "Retrieves film list") — possible because Spring naming is so regular. The pipeline never hard-fails; it degrades.

Both extractors expose the same two methods (`analyze_chunk`, `synthesize_overview`), so `main.py` doesn't know or care which one it got — a strategy-pattern swap.

### 8. `analyzer/main.py` — the conductor

The CLI that runs the five stages in order and prints progress. Its real job is the final **merge**: for every class, combine the parser's hard facts (signatures, endpoints, complexity) with the LLM's soft insights (summaries, descriptions), falling back to javadoc when no insight exists. Output is one hierarchical JSON:

```
metadata → project_overview → statistics → modules → classes → methods
```

`metadata` records *how* the analysis was made (model, chunk count, token strategy) — provenance, so results are auditable.

### 9. `qa/engine.py` + `qa/ask.py` — the Q&A layer (LlamaIndex)

Lets you ask the knowledge base questions in English. Three steps:

**Step 1 — JSON → nodes.** The knowledge file is split into ~274 small `TextNode`s, each one a self-contained fact unit: one node per class, one per controller's endpoint list, one per overview facet, one per module's observations, plus stats/hotspots. Granularity is the design decision: nodes small enough to be precise, big enough to be self-contained, each tagged with metadata (module, role, file) for citation.

**Step 2 — Retrieval (pluggable).** Vector mode embeds nodes and finds semantically similar ones (local HuggingFace model preferred — free and private; OpenAI embeddings as an option). If embeddings aren't available it falls back to **BM25** — classic keyword ranking, no keys, no network, deterministic. Talking point: *BM25 is unusually strong for code Q&A because questions contain exact identifiers like "RentalController" — keyword match nails those.*

**Step 3 — Answering.** With an Anthropic key, the top-k retrieved nodes go to Claude with a prompt that says *answer ONLY from this context* — that grounding is what prevents hallucination. Without a key, you get an extractive answer: the ranked evidence nodes with scores, still genuinely useful.

**The subtle design win:** this indexes the *extracted knowledge*, not raw source files. Retrieval runs over dense, already-curated text, so recall per token beats chunked-source RAG — and every answer automatically inherits the analysis layer (roles, complexity, observations).

---

## Likely Interview Questions — Suggested Answers

**"Why parse before calling the LLM? Why not just send the files?"**
Three reasons: cost (digests are ~4–5× smaller), accuracy (signatures and complexity come from a real parser — exact, never hallucinated), and scale (raw files would need many more chunks and lose cross-file coherence). The division of labor: deterministic facts from the parser, semantic understanding from the LLM.

**"How do you guarantee you never exceed token limits?"**
Hard per-chunk budget (12K) enforced at packing time, a conservative estimator that over-counts, digests instead of raw code, and a truncation guard for any single oversized class. Largest chunk on this repo: ~8.3K — comfortable headroom.

**"Why map/reduce instead of one big prompt?"**
One big prompt doesn't scale past the context window and degrades on long inputs ("lost in the middle"). Map/reduce keeps every call small and focused, scales to arbitrary repo size, parallelizes, and the reduce step still produces a global view — built from evidence, not from skimming.

**"How is the output machine-readable?"**
Schema-enforced via Pydantic + tool-calling — validated JSON by construction, plus temperature 0 for reproducibility. No free-text parsing exists in the pipeline.

**"What did the analysis actually find?"**
Modular monolith, 8 vertical service slices, 70 secured REST endpoints, average complexity 1.87 — and the interesting bit: all 12 methods with cc > 10 are hand-rolled `equals()` methods on big DTOs (max 16). One Lombok annotation (`@EqualsAndHashCode`) would eliminate the entire hotspot list.

**"What are the limitations?"**
Java-only parsing (javalang predates records/sealed types — unparseable files are skipped); token estimation is heuristic by design; complexity counts `&&`/`||` textually rather than via a full control-flow graph; tests excluded by default. Each has a documented upgrade path.

**"How would you extend it?"**
Call-graph extraction for caller/callee context, tree-sitter for multi-language support, and incremental analysis driven by `git diff` against the cache so only changed files are re-analyzed.

---

## One-Line Summaries (rapid-fire recall)

| File | One line |
|---|---|
| `config.py` | All settings + the conservative token estimator |
| `code_reader.py` | Walks repo, filters to Java sources, loads README/build context |
| `java_parser.py` | AST parsing → classes, signatures, roles, REST mappings, complexity |
| `complexity.py` | Aggregates metrics, hotspots, endpoint inventory — zero LLM tokens |
| `chunker.py` | Digests classes + bin-packs them under the token budget, per module |
| `schemas.py` | Pydantic contracts that force valid JSON from the LLM |
| `llm.py` | Map/reduce Claude calls + caching + heuristic fallback |
| `main.py` | Orchestrates the 5 stages, merges facts + insights into knowledge.json |
| `qa/engine.py` | JSON → 274 nodes → vector/BM25 retrieval → grounded answers |
| `qa/ask.py` | CLI for single questions or interactive Q&A |

---

## Bonus: the Streamlit app (`app.py`)

The web app wraps everything above into a visual interface with three phases:

**Phase 1 — Connect a repository.** The first screen you see. Paste a GitHub URL and click Clone. If the repo is private and the clone fails (403, "could not read Username", etc.), the app detects the auth error, auto-opens the 🔐 Authentication panel, and asks for a Personal Access Token. The token is injected into the HTTPS URL at clone time (`https://<token>@github.com/...`), never saved to disk. You can also point to an already-cloned local path.

**Phase 2 — Configure and run.** The sidebar shows LLM settings (API key, model, temperature), token budget, and options. Click Run — the progress bar updates in real time via the `progress_cb` callback added to `main.py`.

**Phase 3 — Explore results.** Seven tabs render the `knowledge.json`: Overview (metrics + purpose + architecture), Input (file tree + digest card sample), Modules (class-by-class with expandable method details), Endpoints (REST inventory with colored verb badges), Complexity (chart + hotspots), Raw JSON (download + browse), and Q&A (BM25 retrieval + Claude synthesis).

| File | One line |
|---|---|
| `app.py` | Streamlit UI: clone flow with auth handling → sidebar config → 7 result tabs |
| `.env.example` | All config keys with defaults — copy to `.env` to use |
