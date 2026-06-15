"""Repository reader: walks the codebase and loads relevant source files."""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List

from .config import AnalyzerConfig


@dataclass
class SourceFile:
    path: str          # path relative to repo root
    content: str
    loc: int           # non-blank lines of code
    is_test: bool


def _is_excluded(path: Path, cfg: AnalyzerConfig) -> bool:
    return any(part in cfg.exclude_dirs for part in path.parts)


def read_context_files(repo_root: str, cfg: AnalyzerConfig) -> dict:
    """Read project-level context files (README, build files) used to ground
    the LLM's project overview."""
    out = {}
    for name in cfg.context_files:
        p = Path(repo_root) / name
        if p.exists():
            out[name] = p.read_text(encoding="utf-8", errors="replace")
    return out


def iter_source_files(repo_root: str, cfg: AnalyzerConfig) -> Iterator[SourceFile]:
    root = Path(repo_root)
    for dirpath, dirnames, filenames in os.walk(root):
        dpath = Path(dirpath)
        # prune excluded directories in-place for efficiency
        dirnames[:] = [d for d in dirnames if d not in cfg.exclude_dirs]
        for fname in sorted(filenames):
            fpath = dpath / fname
            if fpath.suffix not in cfg.include_extensions:
                continue
            if _is_excluded(fpath.relative_to(root), cfg):
                continue
            rel = str(fpath.relative_to(root))
            is_test = "/test/" in f"/{rel}" or rel.startswith("src/test")
            if is_test and not cfg.include_tests:
                continue
            content = fpath.read_text(encoding="utf-8", errors="replace")
            loc = sum(1 for line in content.splitlines() if line.strip())
            yield SourceFile(path=rel, content=content, loc=loc, is_test=is_test)


def read_codebase(repo_root: str, cfg: AnalyzerConfig) -> List[SourceFile]:
    files = list(iter_source_files(repo_root, cfg))
    files.sort(key=lambda f: f.path)
    return files
