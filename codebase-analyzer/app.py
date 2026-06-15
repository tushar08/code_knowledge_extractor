"""
Streamlit app — Codebase Knowledge Extractor

Run:  streamlit run app.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# Make sure our package is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyzer.chunker import build_chunks, class_digest
from analyzer.code_reader import read_codebase, read_context_files
from analyzer.complexity import compute_metrics, module_of
from analyzer.config import AnalyzerConfig
from analyzer.java_parser import parse_java_file
from analyzer.llm import make_extractor
from analyzer.main import run as run_pipeline

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLONE_DIR = Path("./repos")        # all cloned repos live here

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Codebase Knowledge Extractor",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    .metric-card {
        background: var(--background-secondary, #f8f9fa);
        border-radius: 10px; padding: 16px 20px;
        border: 1px solid var(--border-color, #e0e0e0);
    }
    .metric-card h3 { margin: 0 0 2px; font-size: 28px; }
    .metric-card p  { margin: 0; font-size: 13px; opacity: 0.7; }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .endpoint-badge {
        display: inline-block; padding: 2px 8px; border-radius: 4px;
        font-size: 12px; font-weight: 600; margin-right: 6px; color: white;
    }
    .badge-GET    { background: #22863a; }
    .badge-POST   { background: #0366d6; }
    .badge-PUT    { background: #e36209; }
    .badge-DELETE { background: #cb2431; }
    .badge-PATCH  { background: #6f42c1; }
    .clone-box {
        background: var(--background-secondary, #f8f9fa);
        border-radius: 12px; padding: 28px 32px;
        border: 1px solid var(--border-color, #e0e0e0);
        max-width: 680px; margin: 40px auto;
    }
    .clone-box h2 { margin-top: 0; }
    .auth-warning {
        background: #FFF3CD; color: #856404; border-radius: 8px;
        padding: 16px 20px; margin: 12px 0; border: 1px solid #FFEEBA;
    }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# Git helpers
# ═══════════════════════════════════════════════════════════════════════════

def _repo_slug(url: str) -> str:
    """Extract 'owner-repo' from a git URL for the local folder name."""
    url = url.rstrip("/").removesuffix(".git")
    parts = url.replace(":", "/").split("/")
    if len(parts) >= 2:
        return f"{parts[-2]}-{parts[-1]}"
    return parts[-1] if parts else "repo"


def _inject_token_into_url(url: str, token: str) -> str:
    """Insert a PAT into an HTTPS git URL:
       https://github.com/owner/repo  ->
       https://<token>@github.com/owner/repo
    """
    if not token:
        return url
    return re.sub(r"^(https?://)", rf"\1{token}@", url)


def _is_auth_error(stderr: str) -> bool:
    markers = [
        "Authentication failed",
        "could not read Username",
        "terminal prompts disabled",
        "403",
        "401",
        "Invalid username or password",
        "remote: Repository not found",
        "fatal: repository.*not found",
        "Permission denied",
    ]
    low = stderr.lower()
    return any(m.lower() in low for m in markers)


def clone_repo(url: str, dest: str, token: str = "",
               branch: str = "", depth: int = 1) -> dict:
    """Clone a git repository and return status.

    Returns:
        {"ok": bool, "path": str, "error": str, "auth_error": bool,
         "stderr": str}
    """
    clone_url = _inject_token_into_url(url, token) if token else url

    cmd = ["git", "clone"]
    if depth:
        cmd += ["--depth", str(depth)]
    if branch:
        cmd += ["--branch", branch]
    cmd += [clone_url, dest]

    # GIT_TERMINAL_PROMPT=0 makes git fail fast instead of hanging for input
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    result = subprocess.run(cmd, capture_output=True, text=True, env=env,
                            timeout=120)

    stderr = result.stderr or ""
    # Scrub the token out of any error messages
    if token:
        stderr = stderr.replace(token, "***")

    if result.returncode == 0:
        return {"ok": True, "path": dest, "error": "", "auth_error": False,
                "stderr": stderr}
    return {"ok": False, "path": dest, "error": stderr.strip(),
            "auth_error": _is_auth_error(stderr), "stderr": stderr}


def validate_repo(path: str) -> dict:
    """Quick-check that the path is a real git repo with source files."""
    p = Path(path)
    if not p.exists():
        return {"valid": False, "reason": "Path does not exist"}
    if not (p / ".git").exists():
        return {"valid": False, "reason": "Not a git repository (.git missing)"}
    java_count = len(list(p.rglob("*.java")))
    if java_count == 0:
        return {"valid": False, "reason": "No .java files found in repository"}
    return {"valid": True, "java_files": java_count}


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1 — Repository setup (always runs first)
# ═══════════════════════════════════════════════════════════════════════════

def _render_repo_setup():
    """Render the clone/connect screen. Returns the validated repo path
    or None if still waiting for user action."""

    # Check if we already have a valid repo from a previous run
    if "repo_ready" in st.session_state and st.session_state["repo_ready"]:
        rp = st.session_state["repo_path"]
        v = validate_repo(rp)
        if v["valid"]:
            return rp

    st.title("🔬 Codebase Knowledge Extractor")

    st.markdown('<div class="clone-box">', unsafe_allow_html=True)
    st.markdown("### 📂 Connect a repository")
    st.caption("Provide a Git URL to clone, or point to an already-cloned local path.")

    # ── Source choice ─────────────────────────────────────────────────
    source = st.radio("Source", ["Clone from URL", "Use local path"],
                      horizontal=True, label_visibility="collapsed")

    if source == "Use local path":
        local_path = st.text_input(
            "Local repo path",
            value=st.session_state.get("repo_path", ""),
            placeholder="/home/user/my-java-project",
        )
        if st.button("✅ Use this repo", use_container_width=True):
            v = validate_repo(local_path)
            if v["valid"]:
                st.session_state["repo_path"] = local_path
                st.session_state["repo_ready"] = True
                st.rerun()
            else:
                st.error(f"Invalid repository: {v['reason']}")
        st.markdown("</div>", unsafe_allow_html=True)
        return None

    # ── Clone from URL flow ───────────────────────────────────────────
    cfg = AnalyzerConfig()
    default_url = cfg.repo_path if cfg.repo_path.startswith("http") else \
                  "https://github.com/tushar08/spring-rest-sakila_tvr"

    repo_url = st.text_input(
        "Repository URL",
        value=st.session_state.get("clone_url", default_url),
        placeholder="https://github.com/owner/repo",
        help="HTTPS or SSH URL. Private repos need a token below.",
    )
    st.session_state["clone_url"] = repo_url

    branch = st.text_input(
        "Branch (optional)",
        value="",
        placeholder="main (default branch if empty)",
    )

    # ── Auth section — always visible, expanded after auth failure ────
    show_auth = st.session_state.get("clone_auth_error", False)

    with st.expander("🔐 Authentication (for private repos)", expanded=show_auth):
        if show_auth:
            st.markdown(
                '<div class="auth-warning">'
                '⚠️ <strong>Authentication required.</strong> '
                'The clone failed because the repo is private or the URL '
                'needs credentials. Enter a Personal Access Token below.'
                '</div>',
                unsafe_allow_html=True,
            )

        st.caption(
            "For **GitHub**: Settings → Developer Settings → Personal Access Tokens → "
            "Generate (classic) with `repo` scope.  \n"
            "For **GitLab**: Settings → Access Tokens with `read_repository`.  \n"
            "For **Bitbucket**: Settings → App Passwords with `Repositories: Read`."
        )
        git_token = st.text_input(
            "Personal Access Token (PAT)",
            type="password",
            value=st.session_state.get("git_token", ""),
            help="Injected into the HTTPS URL at clone time. Never stored to disk.",
        )
        st.session_state["git_token"] = git_token

    # ── Clone / reclone buttons ───────────────────────────────────────
    dest = str(CLONE_DIR / _repo_slug(repo_url)) if repo_url else ""
    already_cloned = Path(dest).exists() if dest else False

    col1, col2 = st.columns([3, 1])
    with col1:
        clone_btn = st.button(
            "📥 Clone repository" if not already_cloned else "📥 Clone repository",
            type="primary", use_container_width=True,
        )
    with col2:
        reclone_btn = False
        if already_cloned:
            reclone_btn = st.button("🔄 Re-clone", use_container_width=True,
                                    help="Delete existing clone and fetch fresh")

    if already_cloned and not clone_btn and not reclone_btn:
        v = validate_repo(dest)
        if v["valid"]:
            st.success(f"Repository already cloned at `{dest}` "
                       f"({v['java_files']} Java files)")
            if st.button("▶️ Continue with this clone", use_container_width=True):
                st.session_state["repo_path"] = dest
                st.session_state["repo_ready"] = True
                st.session_state["clone_auth_error"] = False
                st.rerun()

    # ── Execute clone ─────────────────────────────────────────────────
    if clone_btn or reclone_btn:
        if not repo_url:
            st.error("Please enter a repository URL.")
        else:
            if reclone_btn and Path(dest).exists():
                shutil.rmtree(dest)

            CLONE_DIR.mkdir(parents=True, exist_ok=True)

            with st.spinner(f"Cloning `{repo_url}` ..."):
                result = clone_repo(
                    url=repo_url,
                    dest=dest,
                    token=git_token,
                    branch=branch,
                )

            if result["ok"]:
                v = validate_repo(dest)
                if v["valid"]:
                    st.success(f"Cloned successfully — "
                               f"{v['java_files']} Java files found in `{dest}`")
                    st.session_state["repo_path"] = dest
                    st.session_state["repo_ready"] = True
                    st.session_state["clone_auth_error"] = False
                    st.rerun()
                else:
                    st.warning(f"Cloned, but: {v['reason']}")
            else:
                if result["auth_error"]:
                    st.session_state["clone_auth_error"] = True
                    st.error(
                        "**Authentication failed.** The repository may be private "
                        "or the URL is incorrect. Expand the 🔐 Authentication "
                        "section above and enter a valid Personal Access Token."
                    )
                    with st.expander("Clone error details"):
                        st.code(result["error"], language="text")
                    # Clean up the partial clone directory
                    if Path(dest).exists():
                        shutil.rmtree(dest)
                    st.rerun()
                else:
                    st.error(f"Clone failed: {result['error']}")
                    with st.expander("Full error output"):
                        st.code(result["stderr"], language="text")
                    if Path(dest).exists():
                        shutil.rmtree(dest)

    st.markdown("</div>", unsafe_allow_html=True)
    return None


# Run the repo setup phase
repo_path = _render_repo_setup()
if repo_path is None:
    st.stop()


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2 — Sidebar configuration + analysis run
# ═══════════════════════════════════════════════════════════════════════════

cfg = AnalyzerConfig()

with st.sidebar:
    st.title("⚙️ Configuration")

    # ── Repo info (read-only, with change button) ─────────────────────
    st.subheader("Repository")
    st.code(repo_path, language=None)
    if st.button("↩ Change repository", use_container_width=True):
        st.session_state["repo_ready"] = False
        st.session_state.pop("knowledge", None)
        st.rerun()

    v = validate_repo(repo_path)
    if v["valid"]:
        st.caption(f"✅ {v['java_files']} Java files detected")
    else:
        st.error(v["reason"])

    st.divider()

    # ── LLM ───────────────────────────────────────────────────────────
    st.subheader("LLM settings")
    api_key = st.text_input("Anthropic API key",
                            value=cfg.anthropic_api_key,  # empty if placeholder
                            type="password",
                            placeholder="sk-ant-api03-... (from .env or paste here)",
                            help="Required for LLM descriptions and Q&A synthesis. "
                                 "Set in `.env` or paste here. Not stored.")
    # Strip whitespace that can sneak in from copy-paste
    api_key = api_key.strip() if api_key else ""
    if api_key:
        st.caption("✅ API key provided")
    else:
        st.caption("ℹ️ No key — will use heuristic extractor (free, lower quality)")
    model = st.selectbox("Model", [
        "claude-sonnet-4-20250514",
        "claude-haiku-4-5-20251001",
    ], index=0)
    temperature = st.slider("Temperature", 0.0, 1.0, cfg.temperature, 0.1)

    # ── Token budget ──────────────────────────────────────────────────
    st.subheader("Token budget")
    max_tokens = st.number_input("Max tokens per chunk",
                                 value=cfg.max_tokens_per_chunk,
                                 min_value=2000, max_value=50000, step=1000)

    # ── Options ───────────────────────────────────────────────────────
    st.subheader("Options")
    include_tests = st.checkbox("Include test files", value=cfg.include_tests)
    output_path = st.text_input("Output path", value=cfg.output_path)

    st.divider()
    run_button = st.button("🚀 Run analysis", type="primary",
                           use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def metric_card(label: str, value, col):
    col.markdown(f"""<div class="metric-card">
        <h3>{value}</h3><p>{label}</p>
    </div>""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# Run pipeline
# ═══════════════════════════════════════════════════════════════════════════

if run_button:
    if api_key:
        os.environ["ANTHROPIC_API_KEY"] = api_key
    os.environ["LLM_MODEL"] = model
    os.environ["LLM_TEMPERATURE"] = str(temperature)
    os.environ["MAX_TOKENS_PER_CHUNK"] = str(max_tokens)
    os.environ["INCLUDE_TESTS"] = str(include_tests).lower()
    os.environ["OUTPUT_PATH"] = output_path

    progress_bar = st.progress(0, text="Starting ...")

    def on_progress(stage, detail, pct):
        progress_bar.progress(min(pct, 1.0), text=f"**[{stage}]** {detail}")

    try:
        knowledge = run_pipeline(repo_path, output_path, include_tests,
                                 progress_cb=on_progress)
        st.session_state["knowledge"] = knowledge
        progress_bar.progress(1.0, text="**Done!**")
        st.success(
            f"Analysis complete — "
            f"{Path(output_path).stat().st_size // 1024} KB "
            f"written to `{output_path}`"
        )
    except Exception as e:
        st.error(f"Pipeline failed: {e}")
        import traceback
        with st.expander("Traceback"):
            st.code(traceback.format_exc())
        st.stop()

# Load from disk if a previous run exists
if "knowledge" not in st.session_state and Path(output_path).exists():
    st.session_state["knowledge"] = json.loads(Path(output_path).read_text())


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3 — Results display
# ═══════════════════════════════════════════════════════════════════════════

st.title("🔬 Codebase Knowledge Extractor")
st.caption("Analyzes a Java codebase with an LLM and outputs structured knowledge")

if "knowledge" not in st.session_state:
    st.info("Click **🚀 Run analysis** in the sidebar to analyze the repository, "
            "or place a `knowledge.json` at the output path to view existing results.")
    st.stop()

k = st.session_state["knowledge"]

# ── Tabs ──────────────────────────────────────────────────────────────────
tab_overview, tab_input, tab_modules, tab_endpoints, tab_complexity, \
    tab_json, tab_qa, tab_telemetry = st.tabs([
        "📋 Overview", "📂 Input", "📦 Modules", "🌐 Endpoints",
        "📊 Complexity", "🗂️ Raw JSON", "💬 Q&A", "📡 Telemetry",
    ])


# ── Tab 1: Overview ──────────────────────────────────────────────────────
with tab_overview:
    ov = k.get("project_overview", {})
    stats = k.get("statistics", {}).get("totals", {})

    cols = st.columns(5)
    for col, (label, key) in zip(cols, [
        ("Source files", "files"), ("Classes", "classes"),
        ("Methods", "methods"), ("LOC", "lines_of_code"),
        ("REST endpoints", "rest_endpoints"),
    ]):
        metric_card(label, f"{stats.get(key, 0):,}", col)

    st.markdown("---")

    if ov.get("purpose"):
        st.subheader("Purpose")
        st.write(ov["purpose"])

    col1, col2 = st.columns(2)
    with col1:
        if ov.get("functionality"):
            st.subheader("Functionality")
            for f in ov["functionality"]:
                st.markdown(f"- {f}")
        if ov.get("design_patterns"):
            st.subheader("Design patterns")
            for p in ov["design_patterns"]:
                st.markdown(f"- {p}")
    with col2:
        if ov.get("architecture"):
            st.subheader("Architecture")
            st.write(ov["architecture"])
        if ov.get("technology_stack"):
            st.subheader("Technology stack")
            for t in ov["technology_stack"]:
                st.markdown(f"- {t}")

    if ov.get("noteworthy_aspects"):
        st.subheader("Noteworthy aspects")
        for n in ov["noteworthy_aspects"]:
            st.info(n, icon="💡")

    meta = k.get("metadata", {})
    with st.expander("Analysis metadata"):
        mc1, mc2 = st.columns(2)
        mc1.markdown(f"**Repository:** `{meta.get('repository', '?')}`")
        mc1.markdown(f"**Analyzed at:** {meta.get('analyzed_at', '?')}")
        llm_meta = meta.get("llm", {})
        mc2.markdown(f"**LLM:** {llm_meta.get('provider')} / {llm_meta.get('model')}")
        mc2.markdown(f"**Extractor:** {llm_meta.get('mode')}")
        ts = meta.get("token_strategy", {})
        mc2.markdown(
            f"**Chunks sent:** {ts.get('chunks_sent')} "
            f"(budget: {ts.get('max_tokens_per_chunk', 0):,} tokens)"
        )


# ── Tab 2: Input ─────────────────────────────────────────────────────────
with tab_input:
    rp = repo_path
    st.subheader(f"Repository: `{rp}`")

    if Path(rp).exists():
        cfg_view = AnalyzerConfig()
        files = read_codebase(rp, cfg_view)

        col1, col2 = st.columns([1, 2])
        with col1:
            st.markdown(f"**{len(files)} Java source files**")
            tree = {}
            for f in files:
                parts = f.path.split("/")
                node = tree
                for p in parts[:-1]:
                    node = node.setdefault(p, {})
                node[parts[-1]] = f.loc
            st.json(tree, expanded=False)
        with col2:
            st.markdown("**File details**")
            file_data = [{"File": f.path.split("/")[-1],
                          "Path": f.path,
                          "Lines": f.loc} for f in files]
            file_data.sort(key=lambda x: -x["Lines"])
            st.dataframe(file_data, use_container_width=True, height=400,
                         column_config={
                             "Path": st.column_config.TextColumn(width="large"),
                         })

        st.subheader("Sample: what the LLM actually sees")
        st.caption("A 'digest card' — the compact representation sent to Claude "
                   "instead of raw source code")
        sample_file = files[0]
        sample_classes = parse_java_file(sample_file)
        if sample_classes:
            digest_text = class_digest(sample_classes[0])
            st.code(digest_text, language="text")
            raw_len = len(sample_file.content)
            dig_len = len(digest_text)
            st.caption(
                f"Raw file: {raw_len:,} chars → Digest: {dig_len:,} chars "
                f"(**{raw_len / max(dig_len, 1):.1f}× reduction**)"
            )
    else:
        st.warning("Repository path not found on disk.")


# ── Tab 3: Modules ───────────────────────────────────────────────────────
with tab_modules:
    modules = k.get("modules", {})
    mod_names = list(modules.keys())
    selected_mod = st.selectbox(
        "Select module", mod_names,
        format_func=lambda m: f"{m} ({modules[m]['class_count']} classes)",
    )
    mod = modules[selected_mod]

    if mod.get("llm_observations"):
        st.subheader("Observations")
        for obs in mod["llm_observations"]:
            st.markdown(f"- {obs}")

    st.subheader(f"Classes ({mod['class_count']})")
    for cls in mod["classes"]:
        role_icons = {
            "controller": "🟢", "service": "🔵", "repository": "🟣",
            "entity": "🟠", "dto": "🟡", "configuration": "⚙️",
            "security": "🔐", "exception_handler": "🚨", "mapper": "🔄",
        }
        icon = role_icons.get(cls["role"], "📄")

        with st.expander(f"{icon} **{cls['name']}** — {cls['role']}"):
            if cls.get("summary"):
                st.markdown(cls["summary"])
            st.caption(f"`{cls['qualified_name']}` · `{cls['file']}`")
            if cls.get("annotations"):
                st.markdown("**Annotations:** " +
                            ", ".join(f"`@{a}`" for a in cls["annotations"]))
            if cls.get("extends"):
                st.markdown(f"**Extends:** `{cls['extends']}`")
            if cls.get("implements"):
                st.markdown("**Implements:** " +
                            ", ".join(f"`{i}`" for i in cls["implements"]))
            if cls.get("methods"):
                st.markdown(f"**Methods ({len(cls['methods'])})**")
                for m in cls["methods"]:
                    ep_badge = ""
                    if m.get("http_endpoint"):
                        verb = m["http_endpoint"].split()[0]
                        ep_badge = (f'<span class="endpoint-badge badge-{verb}">'
                                    f'{m["http_endpoint"]}</span>')
                    cc = m["cyclomatic_complexity"]
                    cc_icon = "🟢" if cc <= 4 else "🟡" if cc <= 10 else "🔴"
                    st.markdown(
                        f"{ep_badge} `{m['signature']}` {cc_icon} cc={cc}",
                        unsafe_allow_html=True,
                    )
                    if m.get("description"):
                        st.caption(f"↳ {m['description']}")


# ── Tab 4: Endpoints ─────────────────────────────────────────────────────
with tab_endpoints:
    endpoints = k.get("statistics", {}).get("rest_endpoints", [])
    st.subheader(f"{len(endpoints)} REST endpoints")
    by_ctrl = {}
    for e in endpoints:
        by_ctrl.setdefault(e["controller"], []).append(e)
    for ctrl, eps in sorted(by_ctrl.items()):
        with st.expander(f"**{ctrl}** ({len(eps)} endpoints)", expanded=True):
            for e in sorted(eps, key=lambda x: x["path"]):
                verb = e["http_method"]
                badge = f'<span class="endpoint-badge badge-{verb}">{verb}</span>'
                st.markdown(
                    f"{badge} `{e['path']}` → `{e['handler']}()`",
                    unsafe_allow_html=True,
                )


# ── Tab 5: Complexity ────────────────────────────────────────────────────
with tab_complexity:
    cc = k.get("statistics", {}).get("cyclomatic_complexity", {})
    hotspots = k.get("statistics", {}).get("complexity_hotspots", [])

    cols = st.columns(4)
    metric_card("Avg complexity", cc.get("average", "?"), cols[0])
    metric_card("Max complexity", cc.get("max", "?"), cols[1])
    metric_card("Methods > 10", cc.get("methods_over_10", "?"), cols[2])
    dist = cc.get("distribution", {})
    metric_card("Simple (1–4)", dist.get("simple_1_4", "?"), cols[3])

    st.markdown("---")
    if dist:
        import pandas as pd
        chart_data = pd.DataFrame({
            "Category": ["Simple (1–4)", "Moderate (5–10)", "Complex (>10)"],
            "Methods": [dist.get("simple_1_4", 0),
                        dist.get("moderate_5_10", 0),
                        dist.get("complex_over_10", 0)],
        })
        st.bar_chart(chart_data, x="Category", y="Methods", color="#7F77DD",
                     use_container_width=True, height=280)
    if hotspots:
        st.subheader("Complexity hotspots")
        st.caption("Methods with the highest cyclomatic complexity — "
                   "refactoring candidates")
        st.dataframe(
            [{"CC": h["cyclomatic_complexity"],
              "Class": h["class"].split(".")[-1],
              "Method": h["method"],
              "File": h["file"]} for h in hotspots],
            use_container_width=True,
            column_config={"CC": st.column_config.NumberColumn(width="small")},
        )


# ── Tab 6: Raw JSON ──────────────────────────────────────────────────────
with tab_json:
    st.subheader("Structured output — knowledge.json")
    json_str = json.dumps(k, indent=2)
    st.download_button("📥 Download knowledge.json", json_str,
                       file_name="knowledge.json", mime="application/json")
    section = st.selectbox("View section", ["Full JSON", "metadata",
                            "project_overview", "statistics", "modules"])
    if section == "Full JSON":
        st.json(k, expanded=False)
    else:
        st.json(k.get(section, {}), expanded=True)


# ── Tab 7: Q&A ──────────────────────────────────────────────────────────
with tab_qa:
    st.subheader("Ask questions about the codebase")
    st.caption("BM25 retrieval over the extracted knowledge · Claude synthesis with API key")

    @st.cache_resource
    def load_qa_engine(knowledge_json: str):
        from qa.engine import build_retriever, load_nodes
        tmp = Path("/tmp/_qa_knowledge.json")
        tmp.write_text(knowledge_json)
        nodes = load_nodes(str(tmp))
        retriever, mode = build_retriever(nodes, mode="bm25", top_k=5)
        return retriever, nodes, mode

    try:
        retriever, nodes, mode = load_qa_engine(json.dumps(k))
        st.caption(f"✅ {len(nodes)} knowledge nodes indexed · retrieval: {mode}")
    except Exception as e:
        st.error(f"Could not load Q&A engine: {e}")
        st.stop()

    preset_qs = [
        "How does authentication work?",
        "Which REST endpoints exist for films?",
        "What are the most complex methods?",
        "What design patterns are used?",
        "How is the project architecture organized?",
    ]

    col1, col2 = st.columns([3, 1])
    with col1:
        question = st.text_input("Your question",
                                 placeholder="e.g. How does JWT authentication work?")
    with col2:
        st.markdown("<br>", unsafe_allow_html=True)
        preset = st.selectbox("Or try a preset", [""] + preset_qs,
                              label_visibility="collapsed")

    q = question or preset
    if q:
        with st.spinner("Searching ..."):
            hits = retriever.retrieve(q)

        effective_key = api_key or AnalyzerConfig().anthropic_api_key

        # ── Top answer (primary source or Claude synthesis) ──────────────
        if effective_key:
            try:
                from qa.engine import answer
                with st.spinner("Claude synthesizing answer ..."):
                    ans_text, usage = answer(q, hits, llm_model=model,
                                            api_key=effective_key)
                st.markdown("### 💬 Answer")
                st.markdown(ans_text)
                st.caption(
                    f"Tokens — input: {usage['input_tokens']:,} · "
                    f"output: {usage['output_tokens']:,} · "
                    f"est. cost: ${(usage['input_tokens']*3 + usage['output_tokens']*15)/1_000_000:.5f}"
                )
            except Exception as e:
                err_msg = str(e)
                if "401" in err_msg or "authentication" in err_msg.lower():
                    st.error("**Invalid API key.** Check the sidebar — "
                             "key should start with `sk-ant-`.")
                else:
                    st.warning(f"Synthesis unavailable ({e}). "
                               "Showing best source below.")
                # Show best hit as fallback
                best = hits[0]
                st.markdown("### 📄 Best matching source")
                st.code(best.node.get_content(), language="text")
        else:
            # No key — show the single best source prominently
            best = hits[0]
            score = f"{best.score:.3f}" if best.score is not None else "—"
            meta  = best.node.metadata
            st.markdown("### 📄 Best match")
            st.caption(f"type: {meta.get('type','?')} · score: {score} · "
                       f"*Add an Anthropic API key in the sidebar for a synthesized answer*")
            st.code(best.node.get_content(), language="text")

        # ── Additional sources — collapsed by default ─────────────────────
        remaining = hits[1:]
        if remaining:
            with st.expander(f"🔍 {len(remaining)} additional sources", expanded=False):
                for i, hit in enumerate(remaining, 2):
                    score = f"{hit.score:.3f}" if hit.score is not None else "—"
                    meta  = hit.node.metadata
                    st.markdown(f"**Source {i}** · type: `{meta.get('type','?')}` "
                                f"· score: {score}")
                    st.code(hit.node.get_content()[:600], language="text")
                    if i < len(hits):
                        st.divider()


# ── Tab 8: Telemetry ─────────────────────────────────────────────────────
with tab_telemetry:
    from analyzer.telemetry import telemetry
    import pandas as pd

    # Wire persist path so events survive Streamlit reruns
    tele_path = str(Path(output_path).parent / "telemetry.json")
    telemetry.set_persist_path(tele_path)

    st.subheader("📡 Token Telemetry & Usage")
    st.caption("Tracks every LLM call — input/output tokens, cost estimates, "
               "and cache savings across the pipeline and Q&A.")

    col1, col2 = st.columns([5, 1])
    with col2:
        if st.button("🗑️ Reset", help="Clear all telemetry data"):
            telemetry.reset()
            st.rerun()

    summary = telemetry.summary()
    events  = telemetry.events_list()

    if not events:
        st.info("No telemetry recorded yet. Run an analysis or ask a Q&A question to see data here.")
        st.stop()

    # ── Top metrics ───────────────────────────────────────────────────────
    cols = st.columns(5)
    metric_card("API calls",          summary["api_calls"],                      cols[0])
    metric_card("Cache hits",         summary["cache_hits"],                     cols[1])
    metric_card("Input tokens",       f"{summary['total_input_tokens']:,}",      cols[2])
    metric_card("Output tokens",      f"{summary['total_output_tokens']:,}",     cols[3])
    metric_card("Est. cost (USD)",    f"${summary['total_cost_usd']:.4f}",       cols[4])

    st.markdown("---")

    # ── Cost breakdown by call type ───────────────────────────────────────
    st.subheader("Breakdown by call type")
    by_type = summary.get("by_call_type", {})
    if by_type:
        rows = []
        for ctype, d in by_type.items():
            rows.append({
                "Call type":    ctype,
                "API calls":    d["calls"] - d["cached"],
                "Cache hits":   d["cached"],
                "Input tokens": d["input"],
                "Output tokens":d["output"],
                "Est. cost ($)":round(d["cost_usd"], 6),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # Bar chart: tokens by type
        chart_rows = [{"Type": r["Call type"],
                       "Input":  r["Input tokens"],
                       "Output": r["Output tokens"]} for r in rows if r["Input tokens"] > 0]
        if chart_rows:
            df_chart = pd.DataFrame(chart_rows).set_index("Type")
            st.bar_chart(df_chart, use_container_width=True, height=220,
                         color=["#7F77DD", "#E8A838"])

    # ── Cache savings banner ──────────────────────────────────────────────
    saved = summary.get("est_tokens_saved_by_cache", 0)
    if saved > 0:
        st.success(
            f"💾 **Cache saved ~{saved:,} tokens** across {summary['cache_hits']} hits "
            f"(re-runs on unchanged code are free)."
        )

    # ── Per-call event log ────────────────────────────────────────────────
    st.subheader("Event log")
    st.caption("Every individual LLM call, newest first")

    import datetime
    log_rows = []
    for e in reversed(events):
        ts = datetime.datetime.fromtimestamp(e["timestamp"]).strftime("%H:%M:%S")
        log_rows.append({
            "Time":    ts,
            "Type":    e["call_type"],
            "Module":  e["module"] or "—",
            "In":      e["input_tokens"]  if not e["cached"] else "—",
            "Out":     e["output_tokens"] if not e["cached"] else "—",
            "Cached":  "✅" if e["cached"] else "❌",
            "Cost($)": f"{e['cost_usd']:.5f}" if not e["cached"] else "0",
        })
    st.dataframe(pd.DataFrame(log_rows), use_container_width=True,
                 hide_index=True, height=300)

    # ── Optimization tips ─────────────────────────────────────────────────
    with st.expander("💡 Optimization tips", expanded=True):
        avg_in = (summary["total_input_tokens"] / max(summary["api_calls"], 1))
        st.markdown(f"""
**Your current usage pattern:**
- Average input tokens per API call: **{avg_in:,.0f}** (budget: `MAX_TOKENS_PER_CHUNK`)
- Cache hit rate: **{100*summary['cache_hits']/max(summary['api_calls']+summary['cache_hits'],1):.0f}%**
  (higher = fewer API calls on re-runs)

**Ways to reduce token usage:**
- ↓ Increase `CHARS_PER_TOKEN` in `.env` to make chunks smaller *(more conservative estimate)*
- ↓ Lower `MAX_TOKENS_PER_CHUNK` to send fewer classes per chunk *(more calls, smaller each)*
- ↓ Keep `INCLUDE_TESTS=false` to skip test files *(large repos)*
- ↑ Lower `LLM_MAX_OUTPUT_TOKENS` *(4096 is already conservative for structured output)*
- ✅ Rely on the cache — re-running on the same codebase should be **free** *(cache hit rate 100%)*

**Cost reference (claude-sonnet-4):** $3/M input · $15/M output
""")

    # ── Download ──────────────────────────────────────────────────────────
    tele_json = json.dumps({"summary": summary, "events": events}, indent=2)
    st.download_button("📥 Download telemetry.json", tele_json,
                       file_name="telemetry.json", mime="application/json")

