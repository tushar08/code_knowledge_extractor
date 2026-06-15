"""Central configuration — loads from .env when present."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Walk up to find .env alongside the project root
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env", override=False)  # don't clobber explicit env vars

_PLACEHOLDER_KEYS = {"sk-ant-your-key-here", "your-key-here", ""}


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _clean_api_key(raw: str) -> str:
    """Strip whitespace and discard known placeholder values."""
    cleaned = raw.strip()
    return "" if cleaned in _PLACEHOLDER_KEYS else cleaned


@dataclass
class AnalyzerConfig:
    # --- LLM settings -------------------------------------------------------
    anthropic_api_key: str = field(default_factory=lambda: _clean_api_key(_env("ANTHROPIC_API_KEY")))
    model_name: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-sonnet-4-20250514"))
    max_output_tokens: int = field(default_factory=lambda: int(_env("LLM_MAX_OUTPUT_TOKENS", "4096")))
    temperature: float = field(default_factory=lambda: float(_env("LLM_TEMPERATURE", "0.0")))

    # --- Token budget -------------------------------------------------------
    max_tokens_per_chunk: int = field(default_factory=lambda: int(_env("MAX_TOKENS_PER_CHUNK", "12000")))
    chars_per_token: float = field(default_factory=lambda: float(_env("CHARS_PER_TOKEN", "3.5")))

    # --- File selection -----------------------------------------------------
    include_extensions: tuple = (".java",)
    context_files: tuple = ("README.md", "build.gradle.kts", "settings.gradle.kts", "pom.xml")
    exclude_dirs: tuple = (".git", "build", "target", "node_modules", ".gradle", "gradle")
    include_tests: bool = field(default_factory=lambda: _env("INCLUDE_TESTS", "false").lower() == "true")

    # --- Output -------------------------------------------------------------
    output_path: str = field(default_factory=lambda: _env("OUTPUT_PATH", "output/knowledge.json"))
    cache_path: str = field(default_factory=lambda: _env("CACHE_PATH", "output/llm_cache.json"))

    # --- Repository ---------------------------------------------------------
    repo_path: str = field(default_factory=lambda: _env("REPO_PATH", ""))

    # Roles that get deep (per-method) LLM analysis.
    deep_analysis_roles: tuple = ("controller", "service", "security", "configuration")

    estimated_tokens: int = field(default=0, init=False)

    def estimate_tokens(self, text: str) -> int:
        return int(len(text) / self.chars_per_token) + 1

    @property
    def has_api_key(self) -> bool:
        return bool(self.anthropic_api_key)
