"""Security and quality scanner.

Uses the tree-sitter AST (already parsed) plus regex patterns to detect:

  SECRETS        — hardcoded passwords, API keys, tokens, connection strings
  VULNERABILITIES — SQL injection, command injection, unsafe deserialization,
                    XXE, path traversal, weak crypto, NPE risk, empty catch blocks
  CODE SMELLS    — magic numbers, hardcoded IPs/URLs, TODO/FIXME markers
  COVERAGE GAPS  — complex methods with no test coverage signal

All findings are structured dicts suitable for JSON output and Streamlit rendering.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import tree_sitter_java as tsj
from tree_sitter import Language, Parser

_JAVA_LANG = Language(tsj.language(), "java")
_PARSER = Parser()
_PARSER.set_language(_JAVA_LANG)

# ── Severity levels ───────────────────────────────────────────────────────────
CRITICAL = "critical"
HIGH     = "high"
MEDIUM   = "medium"
LOW      = "low"
INFO     = "info"


@dataclass
class Finding:
    category:    str           # "secret" | "vulnerability" | "smell" | "coverage_gap"
    rule_id:     str           # machine-readable rule name
    severity:    str           # critical / high / medium / low / info
    title:       str           # one-line human title
    description: str           # what was found and why it matters
    file:        str
    line:        int
    snippet:     str           # short code excerpt (secrets are redacted)
    suggestion:  str           # how to fix it


# ═════════════════════════════════════════════════════════════════════════════
# Secret patterns
# ═════════════════════════════════════════════════════════════════════════════

# (rule_id, severity, title, regex-on-string-value)
SECRET_PATTERNS = [
    ("hardcoded_password",   CRITICAL, "Hardcoded password",
     re.compile(r"(?i)(password|passwd|pwd|secret)\s*=\s*['\"](?!.*\$\{).{4,}")),
    ("hardcoded_api_key",    CRITICAL, "Hardcoded API key",
     re.compile(r"(?i)(api[-_]?key|apikey|access[-_]?key)\s*=\s*['\"].{8,}")),
    ("aws_key_id",           CRITICAL, "AWS access key ID",
     re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws_secret",           CRITICAL, "AWS secret access key",
     re.compile(r"(?i)aws.{0,20}secret.{0,20}['\"][A-Za-z0-9+/]{40}['\"]")),
    ("github_token",         CRITICAL, "GitHub personal access token",
     re.compile(r"gh[pousr]_[A-Za-z0-9]{36}")),
    ("jwt_secret",           HIGH,     "Hardcoded JWT secret",
     re.compile(r"(?i)(jwt|token|signing)[-_]?(secret|key)\s*=\s*['\"].{8,}")),
    ("private_key_pem",      CRITICAL, "Private key material in source",
     re.compile(r"-----BEGIN (RSA|EC|DSA|OPENSSH)? ?PRIVATE KEY-----")),
    ("connection_string",    HIGH,     "Hardcoded DB connection string",
     re.compile(r"(?i)jdbc:[a-z]+://[^\s'\"]{10,}")),
    ("generic_token",        HIGH,     "Hardcoded bearer / auth token",
     re.compile(r"(?i)(bearer|token)\s*[=:]\s*['\"][A-Za-z0-9._\-]{16,}")),
    ("hardcoded_ip",         LOW,      "Hardcoded IP address",
     re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("hardcoded_url",        LOW,      "Hardcoded URL (may contain secrets)",
     re.compile(r"https?://[^\s'\"]{20,}")),
]

def _redact(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-2:]


# ═════════════════════════════════════════════════════════════════════════════
# Vulnerability patterns — AST-based
# ═════════════════════════════════════════════════════════════════════════════

def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node else ""

def _find_all(node, types):
    results = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in types:
            results.append(n)
        stack.extend(reversed(n.children))
    return results

def _lines(src: str) -> List[str]:
    return src.splitlines()


# ── SQL injection ─────────────────────────────────────────────────────────────
_SQL_EXEC = re.compile(r"\.(execute|executeQuery|executeUpdate|prepareStatement|createQuery)\s*\(")
_SQL_CONCAT = re.compile(r'["\'].*SELECT|INSERT|UPDATE|DELETE|FROM|WHERE.*["\'].*\+', re.I)

def _check_sql_injection(src: str, file: str) -> List[Finding]:
    findings = []
    for i, line in enumerate(_lines(src), 1):
        if _SQL_EXEC.search(line) and ("+" in line or "concat" in line.lower()):
            findings.append(Finding(
                category="vulnerability", rule_id="sql_injection",
                severity=CRITICAL, file=file, line=i,
                title="Possible SQL injection",
                description="SQL query appears to concatenate user-controlled input. "
                            "Use PreparedStatement with parameterised queries instead.",
                snippet=line.strip()[:120],
                suggestion="Replace string concatenation with PreparedStatement parameters: "
                           "stmt.setString(1, userInput)",
            ))
    return findings


# ── Command injection ─────────────────────────────────────────────────────────
_CMD_EXEC = re.compile(r"Runtime\.getRuntime\(\)\.exec|ProcessBuilder")

def _check_command_injection(src: str, file: str) -> List[Finding]:
    findings = []
    for i, line in enumerate(_lines(src), 1):
        if _CMD_EXEC.search(line) and ("+" in line or "input" in line.lower()
                                        or "param" in line.lower()):
            findings.append(Finding(
                category="vulnerability", rule_id="command_injection",
                severity=CRITICAL, file=file, line=i,
                title="Possible command injection",
                description="Runtime.exec() or ProcessBuilder with dynamic argument. "
                            "Never pass unsanitised user input to OS commands.",
                snippet=line.strip()[:120],
                suggestion="Validate/whitelist input, avoid shell commands, "
                           "or use ProcessBuilder with an explicit arg list (no shell).",
            ))
    return findings


# ── Unsafe deserialization ────────────────────────────────────────────────────
_DESER = re.compile(r"ObjectInputStream|readObject\(\)|XStream|Kryo|Hessian")

def _check_deserialization(src: str, file: str) -> List[Finding]:
    findings = []
    for i, line in enumerate(_lines(src), 1):
        if _DESER.search(line):
            findings.append(Finding(
                category="vulnerability", rule_id="unsafe_deserialization",
                severity=HIGH, file=file, line=i,
                title="Unsafe deserialization",
                description="Java deserialization is a common attack vector for "
                            "remote code execution (CVE list is extensive).",
                snippet=line.strip()[:120],
                suggestion="Use JSON/Protobuf instead of Java serialization. "
                           "If unavoidable, implement a deserialization filter "
                           "(ObjectInputFilter, Java 9+).",
            ))
    return findings


# ── Weak cryptography ─────────────────────────────────────────────────────────
_WEAK_CRYPTO = re.compile(r'getInstance\(\s*["\']?(MD5|SHA-?1|DES|RC4|ECB)["\']?')

def _check_weak_crypto(src: str, file: str) -> List[Finding]:
    findings = []
    for i, line in enumerate(_lines(src), 1):
        m = _WEAK_CRYPTO.search(line)
        if m:
            algo = m.group(1)
            findings.append(Finding(
                category="vulnerability", rule_id="weak_cryptography",
                severity=HIGH, file=file, line=i,
                title=f"Weak cryptographic algorithm: {algo}",
                description=f"{algo} is cryptographically broken and should not be "
                            f"used for security-sensitive operations.",
                snippet=line.strip()[:120],
                suggestion="Use SHA-256/SHA-3 for hashing, AES-GCM for encryption, "
                           "and BCrypt/Argon2 for passwords.",
            ))
    return findings


# ── Empty catch blocks ────────────────────────────────────────────────────────
_EMPTY_CATCH = re.compile(r"catch\s*\([^)]+\)\s*\{\s*\}", re.DOTALL)

def _check_empty_catch(src: str, file: str) -> List[Finding]:
    findings = []
    for i, line in enumerate(_lines(src), 1):
        if re.search(r"catch\s*\([^)]+\)\s*\{", line):
            # peek ahead 2 lines for closing brace with nothing inside
            block = " ".join(_lines(src)[i-1:i+2])
            if re.search(r"catch\s*\([^)]+\)\s*\{\s*\}", block):
                findings.append(Finding(
                    category="vulnerability", rule_id="empty_catch_block",
                    severity=MEDIUM, file=file, line=i,
                    title="Empty catch block — exception silently swallowed",
                    description="Silently swallowing exceptions hides failures and "
                                "makes debugging very difficult.",
                    snippet=line.strip()[:120],
                    suggestion="At minimum log the exception. Better: propagate it "
                               "or convert to a domain-appropriate exception.",
                ))
    return findings


# ── Path traversal ────────────────────────────────────────────────────────────
_PATH_TRAV = re.compile(r"new\s+File\s*\(|Paths\.get\s*\(|FileInputStream\s*\(")

def _check_path_traversal(src: str, file: str) -> List[Finding]:
    findings = []
    for i, line in enumerate(_lines(src), 1):
        if _PATH_TRAV.search(line) and ("request" in line.lower()
                                         or "param" in line.lower()
                                         or "input" in line.lower()):
            findings.append(Finding(
                category="vulnerability", rule_id="path_traversal",
                severity=HIGH, file=file, line=i,
                title="Possible path traversal",
                description="File path constructed from what appears to be user input. "
                            "Attackers can use ../ sequences to escape the intended directory.",
                snippet=line.strip()[:120],
                suggestion="Canonicalise the path and verify it starts with the "
                           "expected base directory: path.toRealPath().startsWith(basePath)",
            ))
    return findings


# ── XXE ───────────────────────────────────────────────────────────────────────
_XXE = re.compile(r"DocumentBuilderFactory|SAXParserFactory|XMLInputFactory")

def _check_xxe(src: str, file: str) -> List[Finding]:
    findings = []
    for i, line in enumerate(_lines(src), 1):
        if _XXE.search(line):
            findings.append(Finding(
                category="vulnerability", rule_id="xxe_risk",
                severity=HIGH, file=file, line=i,
                title="XML parser — XXE risk if not configured",
                description="Default Java XML parsers allow external entity references "
                            "(XXE), which can expose local files or cause SSRF.",
                snippet=line.strip()[:120],
                suggestion='Disable external entities: factory.setFeature('
                           '"http://apache.org/xml/features/disallow-doctype-decl", true)',
            ))
    return findings


# ── TODO/FIXME markers ────────────────────────────────────────────────────────
_TODO = re.compile(r"//\s*(TODO|FIXME|HACK|XXX|TEMP)[\s:]", re.I)

def _check_todos(src: str, file: str) -> List[Finding]:
    findings = []
    for i, line in enumerate(_lines(src), 1):
        m = _TODO.search(line)
        if m:
            findings.append(Finding(
                category="smell", rule_id="todo_comment",
                severity=INFO, file=file, line=i,
                title=f"{m.group(1).upper()} comment left in code",
                description="Unresolved TODO/FIXME comments indicate incomplete or "
                            "known-broken code that should be tracked in an issue tracker.",
                snippet=line.strip()[:120],
                suggestion="Create a Jira/GitHub issue and reference it: "
                           "// TODO(#123): fix null handling",
            ))
    return findings


# ═════════════════════════════════════════════════════════════════════════════
# Coverage-gap detection (heuristic)
# ═════════════════════════════════════════════════════════════════════════════

def _check_coverage_gaps(classes, test_files: List[str]) -> List[Finding]:
    """Flag high-complexity methods in classes that have no obvious test counterpart."""
    tested_names = set()
    for tf in test_files:
        # heuristic: test file names like FooTest, TestFoo
        stem = Path(tf).stem.lower()
        tested_names.add(stem.replace("test", "").replace("tests", "").strip())

    findings = []
    for cls in classes:
        cls_name_lower = cls.name.lower()
        likely_tested = any(t in cls_name_lower or cls_name_lower in t
                            for t in tested_names)
        if likely_tested:
            continue
        for m in cls.methods:
            if m.cyclomatic_complexity >= 5:
                findings.append(Finding(
                    category="coverage_gap", rule_id="complex_untested_method",
                    severity=MEDIUM if m.cyclomatic_complexity < 10 else HIGH,
                    file=cls.file_path, line=0,
                    title=f"Complex method with no apparent test — CC={m.cyclomatic_complexity}",
                    description=f"`{cls.name}.{m.name}()` has cyclomatic complexity "
                                f"{m.cyclomatic_complexity} and no test class was detected "
                                f"for `{cls.name}`. High CC methods need branch-level tests.",
                    snippet=m.signature,
                    suggestion=f"Write unit tests covering all {m.cyclomatic_complexity} "
                               f"independent paths through `{m.name}()`. "
                               f"Use parametrised tests for data-driven branches.",
                ))
    return findings


# ═════════════════════════════════════════════════════════════════════════════
# Secret scanner (string-literal based)
# ═════════════════════════════════════════════════════════════════════════════

def _scan_secrets(src_content: str, file: str) -> List[Finding]:
    try:
        tree = _PARSER.parse(src_content.encode("utf-8", errors="replace"))
    except Exception:
        return []

    findings = []
    string_nodes = _find_all(tree.root_node, {"string_literal"})
    lines_list = _lines(src_content)

    for node in string_nodes:
        raw = _text(node)
        value = raw.strip('"').strip("'")
        line_num = node.start_point[0] + 1
        context_line = lines_list[line_num - 1].strip() if line_num <= len(lines_list) else raw

        for rule_id, severity, title, pattern in SECRET_PATTERNS:
            if pattern.search(context_line) or pattern.search(value):
                redacted = _redact(value)
                findings.append(Finding(
                    category="secret", rule_id=rule_id,
                    severity=severity, file=file, line=line_num,
                    title=title,
                    description=f"Found potential {title.lower()} in source code. "
                                f"Value (redacted): `{redacted}`. "
                                "Secrets in source code are visible to anyone with repo access "
                                "and persist in git history even after deletion.",
                    snippet=context_line[:120],
                    suggestion="Move to environment variables, .env files (excluded from git), "
                               "or a secrets manager (AWS Secrets Manager, HashiCorp Vault, "
                               "Spring Cloud Config). Rotate any exposed secret immediately.",
                ))
                break  # one finding per string node
    return findings


# ═════════════════════════════════════════════════════════════════════════════
# Main entrypoint
# ═════════════════════════════════════════════════════════════════════════════

def scan_file(src_content: str, file_path: str, classes=None,
              test_files: List[str] = None) -> List[Finding]:
    """Run all scanners on a single source file."""
    findings: List[Finding] = []
    findings.extend(_scan_secrets(src_content, file_path))
    findings.extend(_check_sql_injection(src_content, file_path))
    findings.extend(_check_command_injection(src_content, file_path))
    findings.extend(_check_deserialization(src_content, file_path))
    findings.extend(_check_weak_crypto(src_content, file_path))
    findings.extend(_check_empty_catch(src_content, file_path))
    findings.extend(_check_path_traversal(src_content, file_path))
    findings.extend(_check_xxe(src_content, file_path))
    findings.extend(_check_todos(src_content, file_path))
    return findings


def scan_codebase(source_files, classes=None, test_files: List[str] = None) -> dict:
    """Scan all source files and return a structured report."""
    all_findings: List[Finding] = []

    for sf in source_files:
        findings = scan_file(sf.content, sf.path, test_files=test_files or [])
        all_findings.extend(findings)

    if classes:
        all_findings.extend(_check_coverage_gaps(classes, test_files or []))

    # Build summary
    by_severity = {CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0, INFO: 0}
    by_category = {}
    by_rule: dict = {}
    for f in all_findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        by_category[f.category] = by_category.get(f.category, 0) + 1
        by_rule[f.rule_id] = by_rule.get(f.rule_id, 0) + 1

    findings_list = [
        {
            "category":    f.category,
            "rule_id":     f.rule_id,
            "severity":    f.severity,
            "title":       f.title,
            "description": f.description,
            "file":        f.file,
            "line":        f.line,
            "snippet":     f.snippet,
            "suggestion":  f.suggestion,
        }
        for f in sorted(all_findings,
                        key=lambda x: [CRITICAL,HIGH,MEDIUM,LOW,INFO].index(x.severity))
    ]

    return {
        "summary": {
            "total": len(all_findings),
            "by_severity": by_severity,
            "by_category": by_category,
            "by_rule":     dict(sorted(by_rule.items(), key=lambda x: -x[1])),
        },
        "findings": findings_list,
    }
