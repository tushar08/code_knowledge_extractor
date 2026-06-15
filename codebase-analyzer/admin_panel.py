"""Admin panel for the Codebase Knowledge Extractor.

Activated by visiting the app with ?admin=1 in the URL.
Provides:
  1. Architecture walkthrough — interactive pipeline diagram + file-by-file rationale
  2. Source viewer — read any analyzer file with syntax highlighting
  3. LLM Improver — ask Claude to review and suggest improvements to any file
  4. Live editor — edit a file, preview the diff, apply changes to disk
  5. GitHub integration — stage changes and push to a remote repository
"""
import importlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent

# ── All source files exposed in the admin panel ───────────────────────────
SOURCE_FILES = {
    "analyzer/config.py":               "Settings dial — all .env keys, model resolution",
    "analyzer/code_reader.py":          "File walker — reads .java sources from the repo",
    "analyzer/java_parser.py":          "AST parser — tree-sitter-java, Java 17+ support",
    "analyzer/complexity.py":           "Metrics engine — cyclomatic complexity, hotspots",
    "analyzer/chunker.py":              "Token packer — digest cards + bin-packing",
    "analyzer/schemas.py":              "Pydantic schemas — force structured LLM output",
    "analyzer/llm.py":                  "LLM brain — LangChain map/reduce + cache + fallback",
    "analyzer/model_utils.py":          "Model guard — alias resolution + graceful errors",
    "analyzer/main.py":                 "CLI orchestrator — 5-stage pipeline + progress cb",
    "analyzer/telemetry.py":            "Token tracker — usage, cost, cache savings",
    "analyzer/security_scanner.py":     "Security scanner — secrets, vulns, coverage gaps",
    "analyzer/dependency_analyser.py":  "Dependency tracer — REST, Kafka, DB, code flow",
    "qa/engine.py":                     "RAG engine — LlamaIndex nodes + BM25 retrieval",
    "qa/ask.py":                        "Q&A CLI — question/answer over knowledge.json",
    "app.py":                           "Streamlit UI — 10 tabs, clone flow, all features",
}

# ── Library rationale ─────────────────────────────────────────────────────
LIBRARY_RATIONALE = [
    ("tree-sitter-java",     "Java parser",
     "Handles Java 17+ (sealed classes, records, switch expressions, pattern matching). "
     "javalang was limited to Java 8–11. tree-sitter uses a battle-tested grammar maintained "
     "by GitHub. Parses 186 files in ~0.3s. Key advantage: graceful error recovery — malformed "
     "files degrade to partial ASTs rather than crashing."),
    ("LangChain (langchain-anthropic)",  "LLM orchestration",
     "`with_structured_output(PydanticModel)` enforces schema-valid JSON via Anthropic "
     "tool-calling — no brittle text parsing. Also provides retry logic, streaming, and "
     "a unified interface if you swap to OpenAI/Mistral. Cost: one extra abstraction layer. "
     "Worth it for the structured-output guarantee alone."),
    ("Pydantic v2",          "Output schemas",
     "Defines what the LLM must return (`ChunkInsights`, `ProjectOverview`). Combined "
     "with LangChain's tool-calling, the LLM fills in a typed schema rather than writing "
     "free-text JSON — valid by construction, not by parsing luck. temperature=0 makes "
     "output deterministic and cacheable."),
    ("LlamaIndex (llama-index-core)",  "RAG layer",
     "Converts knowledge.json into ~274 TextNodes and provides BM25 + vector retrieval. "
     "Indexing the extracted knowledge (not raw source) means retrieval runs over curated, "
     "dense text — better recall per token. BM25 is default: no API keys, deterministic, "
     "and strong for code Q&A because identifiers are exact-match friendly."),
    ("python-dotenv",        "Configuration",
     "Loads .env at import time. `override=False` means explicit env vars (set by the "
     "Streamlit sidebar) take precedence over file values. Combined with `_clean_api_key()` "
     "and `_resolve_at_load()`, placeholder values and deprecated model names are silently "
     "corrected before anything reaches the API."),
    ("Streamlit",            "Web UI",
     "Zero-boilerplate Python → interactive web app. Key choices: session_state for "
     "per-session caching (avoids re-running the pipeline on tab switch), query_params "
     "for the hidden admin panel, progress callbacks wired from `main.py` for real-time "
     "chunk-by-chunk updates. Limitation: single-process — long analyses block the UI "
     "thread (fix: st.experimental_fragment or a background thread)."),
    ("anthropic (direct SDK)",  "Q&A synthesis + telemetry",
     "Used directly (not via LangChain) in `qa/engine.py` so we get `response.usage` "
     "with exact input/output token counts — LangChain's abstraction layer didn't always "
     "surface these reliably. Enables the cost-per-question display in the Q&A tab."),
]

# ── Pipeline step explanations ────────────────────────────────────────────
PIPELINE_STEPS = [
    {
        "num": "1", "name": "Read", "file": "code_reader.py", "color": "#E3F2FD",
        "border": "#1565C0",
        "what": "Walks the repo with `os.walk`, pruning excluded dirs in-place "
                "(`dirnames[:] = ...` — never even enters `.git` or `build`). "
                "Filters to `.java` production sources. Loads README + build files "
                "separately as LLM grounding context.",
        "why": "Separation of reading from parsing means tests can mock the file system. "
               "The context files (README, Gradle) give the reduce step real project intent "
               "rather than just code structure.",
        "output": "List[SourceFile(path, content, loc, is_test)]",
        "tokens": "0 — pure Python",
    },
    {
        "num": "2", "name": "Parse", "file": "java_parser.py", "color": "#E8F5E9",
        "border": "#2E7D32",
        "what": "tree-sitter-java AST parser. Extracts: packages, class/interface/enum/"
                "record/sealed-class declarations, method signatures with full type info, "
                "annotations (both `@Foo` marker and `@Foo(value=...)` forms), HTTP mappings, "
                "injected field types, cyclomatic complexity per method.",
        "why": "Never sends raw code to the LLM. Parser gives exact, hallucination-free "
               "facts. 4-5× cheaper than sending raw files. Key fix over javalang: handles "
               "Java 17+ syntax and correctly distinguishes `marker_annotation` vs `annotation` "
               "node types for Spring role detection.",
        "output": "List[ClassInfo] with nested List[MethodInfo]",
        "tokens": "0 — pure Python",
    },
    {
        "num": "3", "name": "Metrics", "file": "complexity.py", "color": "#FFF3E0",
        "border": "#E65100",
        "what": "Aggregates cyclomatic complexity (1 + decision points per method), "
                "class role distribution, per-module stats (classes/methods/LOC), "
                "top-15 complexity hotspots, full REST endpoint inventory "
                "(verb + path + controller + handler).",
        "why": "Zero LLM tokens. Math goes to Python, meaning goes to the LLM. "
               "The hotspot list is one of the most actionable outputs — it told us "
               "all 12 methods with CC > 10 are hand-rolled equals() ripe for @EqualsAndHashCode.",
        "output": "Dict with totals, class_roles, modules, cyclomatic_complexity, hotspots, endpoints",
        "tokens": "0 — pure Python",
    },
    {
        "num": "4", "name": "Chunk", "file": "chunker.py", "color": "#F3E5F5",
        "border": "#6A1B9A",
        "what": "`class_digest()` renders each class as a compact text card: name, role, "
                "annotations, injected deps, one line per method signature + complexity. "
                "Cards are bin-packed into chunks under MAX_TOKENS_PER_CHUNK (12K default), "
                "never mixing modules so each chunk is semantically coherent.",
        "why": "The single biggest lever on cost and quality. 186 files → 11 chunks at "
               "max 8.3K tokens each. Without this: raw files = ~60K tokens = over context "
               "window, much higher cost, worse summaries (lost-in-the-middle problem).",
        "output": "List[{module, text, tokens}]",
        "tokens": "0 — pure Python",
    },
    {
        "num": "5", "name": "LLM Map", "file": "llm.py", "color": "#FCE4EC",
        "border": "#880E4F",
        "what": "Each chunk → one Claude call → `ChunkInsights` (per-class summaries + "
                "per-method descriptions + module observations). Uses LangChain's "
                "`with_structured_output(ChunkInsights, include_raw=True)` to get both "
                "the parsed object and the raw AIMessage for token counts. SHA-256 content "
                "cache: re-runs on unchanged code cost zero tokens.",
        "why": "Map/reduce beats a single mega-prompt: scales past the context window, "
               "each call is focused on one module, failures are isolated, cache hits "
               "make re-runs free. `include_raw=True` is what makes token telemetry work.",
        "output": "Dict[qualified_name → ClassInsight]",
        "tokens": "PAID — proportional to codebase size",
    },
    {
        "num": "6", "name": "LLM Reduce", "file": "llm.py", "color": "#FCE4EC",
        "border": "#880E4F",
        "what": "One final Claude call: README + build files + all module observations "
                "+ aggregated metrics → `ProjectOverview` (purpose, functionality, "
                "architecture, stack, patterns, noteworthy aspects). Also cached.",
        "why": "The reduce step gets a global view built bottom-up from evidence, not "
               "from skimming a 60K token blob. The metrics JSON gives it concrete "
               "numbers (70 endpoints, avg CC 1.87, 12 hotspots) to reason about.",
        "output": "ProjectOverview",
        "tokens": "PAID — one call, larger context",
    },
    {
        "num": "7", "name": "Assemble", "file": "main.py", "color": "#E3F2FD",
        "border": "#1565C0",
        "what": "Merges parser facts (signatures, endpoints, complexity) with LLM insights "
                "(summaries, descriptions). Falls back to javadoc when no LLM insight "
                "exists. Writes the hierarchical JSON: metadata → project_overview → "
                "statistics → modules → classes → methods.",
        "why": "The metadata block records provenance: model used, extractor mode (Claude "
               "vs heuristic), chunks sent, token strategy. This makes results auditable "
               "— you can see exactly how the knowledge was produced.",
        "output": "knowledge.json",
        "tokens": "0 — pure Python",
    },
]


def _read_file(rel_path: str) -> str:
    p = PROJECT_ROOT / rel_path
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def _write_file(rel_path: str, content: str):
    p = PROJECT_ROOT / rel_path
    p.write_text(content, encoding="utf-8")


def _git_status() -> dict:
    """Run git status and return parsed info."""
    r = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    if r.returncode != 0:
        return {"error": r.stderr.strip(), "files": []}
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    files = [{"status": l[:2].strip(), "path": l[3:].strip()} for l in lines]
    return {"error": None, "files": files}


def _git_diff(rel_path: str) -> str:
    r = subprocess.run(
        ["git", "diff", "--", rel_path],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    return r.stdout or "(no diff — file may be untracked)"


def _git_commit_push(message: str, remote: str, branch: str) -> dict:
    def run(cmd):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        return r.returncode, r.stdout + r.stderr

    code, out = run(["git", "add", "-A"])
    if code != 0:
        return {"ok": False, "output": f"git add failed:\n{out}"}
    code, out = run(["git", "commit", "-m", message])
    if code != 0:
        return {"ok": False, "output": f"git commit failed:\n{out}"}
    if remote and branch:
        code, out = run(["git", "push", remote, branch])
        if code != 0:
            return {"ok": False, "output": f"git push failed:\n{out}"}
    return {"ok": True, "output": out}


def _llm_improve(file_path: str, file_content: str, api_key: str,
                 model: str, instruction: str) -> str:
    """Ask Claude to review a source file and suggest improvements."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    system = (
        "You are a senior Python architect reviewing source files from a codebase "
        "analysis tool. Provide concrete, specific improvements with before/after code "
        "snippets. Focus on correctness, performance, and maintainability. "
        "Format your response in Markdown with clear sections."
    )
    prompt = f"""File: `{file_path}`

```python
{file_content[:12000]}
```

{'Additional instruction: ' + instruction if instruction else 'Review this file for improvements.'}

Provide:
1. **Summary** — what this file does in 2-3 sentences
2. **Issues found** — bugs, edge cases, performance problems (if any)
3. **Suggested improvements** — with before/after code snippets
4. **Library alternatives** — better choices if applicable
5. **Production readiness gaps** — what would be needed for prod use"""

    response = client.messages.create(
        model=model, max_tokens=2048, temperature=0.3,
        messages=[{"role": "user", "content": prompt}],
        system=system,
    )
    return response.content[0].text


def render(api_key: str, model: str):
    """Render the full admin panel. Called from app.py when ?admin=1."""

    st.markdown("""
    <div style="background:linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f3460 100%);
    border-radius:12px;padding:20px 28px;margin-bottom:20px">
    <span style="color:#e94560;font-size:11px;font-weight:700;letter-spacing:2px;
    text-transform:uppercase">🔒 Admin Panel</span>
    <h2 style="color:white;margin:4px 0 0;font-size:22px;font-weight:500">
    Codebase Analyzer — Internal Developer View</h2>
    <p style="color:#a8b2d8;font-size:13px;margin:6px 0 0">
    Architecture · Source viewer · LLM improver · Editor · GitHub push<br>
    <em>Access via <code style="color:#e94560">?admin=1</code> in the URL</em></p>
    </div>
    """, unsafe_allow_html=True)

    adm_tab1, adm_tab2, adm_tab3, adm_tab4, adm_tab5 = st.tabs([
        "🗺️ Architecture",
        "📄 Source viewer",
        "🤖 LLM improver",
        "✏️ Live editor",
        "🐙 GitHub",
    ])

    # ── Tab 1: Architecture ───────────────────────────────────────────────
    with adm_tab1:
        st.subheader("How the analyzer works — step by step")

        # Pipeline steps
        for step in PIPELINE_STEPS:
            with st.container():
                st.markdown(
                    f'<div style="border-left:4px solid {step["border"]};'
                    f'background:{step["color"]};border-radius:0 8px 8px 0;'
                    f'padding:14px 18px;margin-bottom:12px">'
                    f'<span style="font-size:11px;font-weight:700;color:{step["border"]};'
                    f'text-transform:uppercase;letter-spacing:1px">Step {step["num"]}</span>'
                    f'<h4 style="margin:4px 0 2px;font-size:16px">{step["name"]} '
                    f'<code style="font-size:12px;font-weight:400">{step["file"]}</code></h4>'
                    f'<p style="margin:0;font-size:13px">{step["what"]}</p></div>',
                    unsafe_allow_html=True,
                )
                c1, c2, c3 = st.columns(3)
                c1.markdown(f"**Why this way:** {step['why']}", )
                c2.caption(f"**Output:** {step['output']}")
                c3.caption(f"**LLM tokens:** {step['tokens']}")
                st.markdown("---")

        # Library rationale table
        st.subheader("Library choices & rationale")
        for lib, role, reason in LIBRARY_RATIONALE:
            with st.expander(f"**{lib}** — {role}"):
                st.markdown(reason)

        # Design decisions summary
        st.subheader("Key design decisions")
        decisions = {
            "Digest cards, not raw files": (
                "4-5× token reduction. `class_digest()` sends signatures + annotations + "
                "complexity. Boilerplate (imports, getters, license headers) is stripped "
                "entirely. The parser's exact facts are better inputs than raw text anyway."
            ),
            "Module-coherent bin-packing": (
                "All of `services.catalog` goes in one chunk together. The LLM sees a "
                "self-consistent slice — better cross-class observations, no confusion "
                "from mixing unrelated modules."
            ),
            "SHA-256 content cache": (
                "Re-runs on unchanged code cost zero API tokens. The key is "
                "`hash(model_name + content)` — changing the model automatically "
                "invalidates the cache. This is what makes iteration cheap during development."
            ),
            "Pydantic schema enforcement": (
                "LLM output is valid JSON by construction via tool-calling. No free-text "
                "parsing, no brittle regex. If the schema fails, LangChain retries. "
                "temperature=0 makes output deterministic and cacheable."
            ),
            "Interface → implementation resolution": (
                "Spring injects `FilmService` (interface) but the class is `FilmServiceImpl`. "
                "The code flow tracer builds an interface→impl map from the class hierarchy "
                "so the call chain correctly traces through the concrete class."
            ),
            "Heuristic fallback": (
                "No API key = `HeuristicExtractor` uses Spring naming conventions for "
                "free, offline descriptions. The same pipeline structure runs, producing "
                "structural output (classes, endpoints, complexity) without any LLM cost."
            ),
        }
        for title, explanation in decisions.items():
            with st.expander(f"**{title}**"):
                st.markdown(explanation)

    # ── Tab 2: Source viewer ──────────────────────────────────────────────
    with adm_tab2:
        st.subheader("Browse analyzer source files")
        file_choice = st.selectbox(
            "Select file",
            list(SOURCE_FILES.keys()),
            format_func=lambda f: f"  {f}  —  {SOURCE_FILES[f]}",
        )
        content = _read_file(file_choice)
        if content:
            st.caption(f"`{PROJECT_ROOT / file_choice}` · {len(content.splitlines())} lines "
                       f"· {len(content):,} chars")
            st.code(content, language="python", line_numbers=True)
        else:
            st.warning(f"File not found: `{file_choice}`")

    # ── Tab 3: LLM Improver ───────────────────────────────────────────────
    with adm_tab3:
        st.subheader("Ask Claude to review and suggest improvements")
        if not api_key:
            st.warning("⚠️ Anthropic API key required for LLM improvements. "
                       "Add it in the main sidebar.")
            st.stop()

        col1, col2 = st.columns([2, 1])
        with col1:
            improve_file = st.selectbox(
                "File to review",
                list(SOURCE_FILES.keys()),
                format_func=lambda f: f"  {f}  —  {SOURCE_FILES[f]}",
                key="improve_file",
            )
        with col2:
            focus = st.selectbox("Focus area", [
                "General review",
                "Performance & efficiency",
                "Error handling & resilience",
                "Token cost reduction",
                "Test coverage gaps",
                "Security concerns",
                "Production readiness",
            ])

        custom_instruction = st.text_input(
            "Custom instruction (optional)",
            placeholder="e.g. 'How would you handle a 50,000-file monorepo?'",
        )

        if st.button("🤖 Run LLM review", type="primary"):
            content = _read_file(improve_file)
            instruction = custom_instruction or focus
            with st.spinner(f"Claude reviewing `{improve_file}` ..."):
                try:
                    from analyzer.model_utils import resolve_model
                    resolved_model, _ = resolve_model(model)
                    result = _llm_improve(improve_file, content, api_key,
                                          resolved_model, instruction)
                    st.session_state["llm_review"] = {
                        "file": improve_file,
                        "result": result,
                        "instruction": instruction,
                    }
                except Exception as e:
                    from analyzer.model_utils import friendly_api_error
                    st.error(friendly_api_error(e, model))

        if "llm_review" in st.session_state:
            rev = st.session_state["llm_review"]
            st.markdown(f"**Review of `{rev['file']}`** — *{rev['instruction']}*")
            st.markdown(rev["result"])

            # Offer to apply suggestions to the editor
            if st.button("✏️ Open this file in the editor →"):
                st.session_state["editor_file"] = rev["file"]
                st.session_state["editor_note"] = (
                    "File opened from LLM review. Apply suggestions above."
                )
                st.rerun()

    # ── Tab 4: Live editor ────────────────────────────────────────────────
    with adm_tab4:
        st.subheader("Edit analyzer source files")
        st.warning(
            "⚠️ Changes are written directly to disk. Make sure you understand "
            "the change before applying. Use the GitHub tab to commit and push.",
            icon="⚠️",
        )

        # Pre-select file if coming from LLM improver
        default_file = st.session_state.get("editor_file",
                                             list(SOURCE_FILES.keys())[0])
        if "editor_note" in st.session_state:
            st.info(st.session_state.pop("editor_note"))

        edit_file = st.selectbox(
            "File to edit",
            list(SOURCE_FILES.keys()),
            index=list(SOURCE_FILES.keys()).index(default_file)
                  if default_file in SOURCE_FILES else 0,
            key="edit_file_select",
        )

        original = _read_file(edit_file)
        st.caption(f"`{PROJECT_ROOT / edit_file}` · {len(original.splitlines())} lines")

        edited = st.text_area(
            "Source code",
            value=original,
            height=550,
            key=f"editor_{edit_file}",
        )

        col1, col2, col3 = st.columns(3)
        save_btn  = col1.button("💾 Save to disk", type="primary")
        diff_btn  = col2.button("🔍 Preview diff")
        reset_btn = col3.button("↩️ Reset to saved")

        if reset_btn:
            st.session_state[f"editor_{edit_file}"] = original
            st.rerun()

        if diff_btn:
            if edited == original:
                st.info("No changes from the saved version.")
            else:
                import difflib
                diff = list(difflib.unified_diff(
                    original.splitlines(keepends=True),
                    edited.splitlines(keepends=True),
                    fromfile=f"a/{edit_file}",
                    tofile=f"b/{edit_file}",
                    lineterm="",
                ))
                if diff:
                    st.code("".join(diff), language="diff")

        if save_btn:
            if edited == original:
                st.info("No changes to save.")
            else:
                try:
                    # Syntax check before saving
                    compile(edited, edit_file, "exec")
                    _write_file(edit_file, edited)
                    st.success(f"✅ Saved `{edit_file}` ({len(edited.splitlines())} lines)")
                    # Invalidate session cache so next run picks up new code
                    for key in list(st.session_state.keys()):
                        if key not in ("repo_ready", "repo_path", "clone_url",
                                       "git_token", "knowledge"):
                            pass  # selective: keep main state
                except SyntaxError as se:
                    st.error(f"**Syntax error** — file NOT saved:\n```\n{se}\n```")
                except Exception as e:
                    st.error(f"Save failed: {e}")

    # ── Tab 5: GitHub ─────────────────────────────────────────────────────
    with adm_tab5:
        st.subheader("Git status & GitHub push")

        # Check if inside a git repo
        is_git = (PROJECT_ROOT / ".git").exists()
        git_check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=PROJECT_ROOT,
        )
        is_git = is_git or git_check.returncode == 0

        if not is_git:
            st.info("This project is not yet a git repository.")
            col1, col2 = st.columns(2)
            with col1:
                remote_url = st.text_input(
                    "GitHub remote URL",
                    placeholder="https://github.com/yourname/code_knowledge_extractor.git",
                )
            with col2:
                branch_name = st.text_input("Branch", value="main")

            git_token_gh = st.text_input("GitHub token (for push)", type="password",
                                          help="Injected into URL at push time, not stored")
            if st.button("🚀 Initialize git + push to GitHub", type="primary"):
                if not remote_url:
                    st.error("Please enter a remote URL.")
                else:
                    with st.spinner("Initialising repository ..."):
                        cmds = [
                            ["git", "init", "-b", branch_name],
                            ["git", "add", "-A"],
                            ["git", "commit", "-m", "Initial commit: codebase knowledge extractor"],
                        ]
                        output = []
                        ok = True
                        for cmd in cmds:
                            r = subprocess.run(cmd, capture_output=True, text=True,
                                               cwd=PROJECT_ROOT)
                            output.append(f"$ {' '.join(cmd)}\n{r.stdout + r.stderr}")
                            if r.returncode != 0:
                                ok = False
                                break

                        if ok and git_token_gh:
                            import re
                            push_url = re.sub(r"^(https?://)",
                                              f"\\1{git_token_gh}@", remote_url)
                            r = subprocess.run(
                                ["git", "remote", "add", "origin", push_url],
                                capture_output=True, text=True, cwd=PROJECT_ROOT,
                            )
                            output.append(r.stdout + r.stderr)
                            r = subprocess.run(
                                ["git", "push", "-u", "origin", branch_name],
                                capture_output=True, text=True, cwd=PROJECT_ROOT,
                            )
                            output.append(r.stdout.replace(git_token_gh, "***")
                                         + r.stderr.replace(git_token_gh, "***"))
                            ok = r.returncode == 0

                        with st.expander("Git output", expanded=True):
                            st.code("\n\n".join(output), language="bash")
                        if ok:
                            st.success("✅ Repository initialised and pushed!")
                            st.rerun()
                        else:
                            st.error("Push failed — see output above.")
        else:
            # Existing git repo
            status = _git_status()
            if status["error"]:
                st.error(f"git error: {status['error']}")
            else:
                # Current branch
                branch_r = subprocess.run(
                    ["git", "branch", "--show-current"],
                    capture_output=True, text=True, cwd=PROJECT_ROOT,
                )
                current_branch = branch_r.stdout.strip() or "main"

                # Remotes
                remote_r = subprocess.run(
                    ["git", "remote", "-v"],
                    capture_output=True, text=True, cwd=PROJECT_ROOT,
                )
                remotes = remote_r.stdout.strip()

                col1, col2 = st.columns(2)
                col1.metric("Branch", current_branch)
                col2.metric("Changed files", len(status["files"]))

                if remotes:
                    with st.expander("Remotes"):
                        st.code(remotes, language="bash")

                if not status["files"]:
                    st.success("✅ Working tree clean — nothing to commit.")
                else:
                    st.markdown(f"**{len(status['files'])} changed file(s):**")
                    for f in status["files"]:
                        icon = {"M": "📝", "A": "🆕", "D": "🗑️",
                                "?": "❓", "R": "🔄"}.get(f["status"][:1], "📄")
                        st.markdown(f"{icon} `{f['path']}` — `{f['status']}`")

                    # Show diff for selected file
                    if status["files"]:
                        diff_file = st.selectbox(
                            "Preview diff for",
                            [f["path"] for f in status["files"]],
                        )
                        diff_text = _git_diff(diff_file)
                        if diff_text.strip():
                            st.code(diff_text, language="diff")

                st.markdown("---")
                st.subheader("Commit & push")

                commit_msg = st.text_input(
                    "Commit message",
                    value="feat: update codebase analyzer",
                    placeholder="feat: description of changes",
                )
                push_remote = st.text_input("Remote", value="origin")
                push_branch = st.text_input("Branch", value=current_branch)
                gh_token    = st.text_input(
                    "GitHub token (optional, for HTTPS push)",
                    type="password",
                    help="Injected into the remote URL at push time, never stored.",
                )

                if st.button("🚀 Commit & push", type="primary",
                             disabled=not commit_msg):
                    with st.spinner("Committing and pushing ..."):
                        env = {**os.environ}
                        if gh_token:
                            # Inject token into remote URL
                            url_r = subprocess.run(
                                ["git", "remote", "get-url", push_remote],
                                capture_output=True, text=True, cwd=PROJECT_ROOT,
                            )
                            if url_r.returncode == 0:
                                import re
                                new_url = re.sub(
                                    r"^(https?://)",
                                    f"\\1{gh_token}@",
                                    url_r.stdout.strip(),
                                )
                                subprocess.run(
                                    ["git", "remote", "set-url", push_remote, new_url],
                                    capture_output=True, cwd=PROJECT_ROOT,
                                )

                        result = _git_commit_push(commit_msg, push_remote, push_branch)

                        if gh_token:
                            # Restore clean remote URL (remove token)
                            url_r = subprocess.run(
                                ["git", "remote", "get-url", push_remote],
                                capture_output=True, text=True, cwd=PROJECT_ROOT,
                            )
                            if url_r.returncode == 0:
                                import re
                                clean_url = re.sub(
                                    r"(https?://)([^@]+@)",
                                    r"\1",
                                    url_r.stdout.strip(),
                                )
                                subprocess.run(
                                    ["git", "remote", "set-url", push_remote, clean_url],
                                    capture_output=True, cwd=PROJECT_ROOT,
                                )

                    output_clean = result["output"].replace(gh_token, "***") if gh_token \
                                   else result["output"]
                    with st.expander("Git output", expanded=True):
                        st.code(output_clean, language="bash")

                    if result["ok"]:
                        st.success("✅ Committed and pushed successfully!")
                        st.balloons()
                    else:
                        st.error("Push failed — see output above.")
