"""Structural parser for Java sources.

Uses the `javalang` AST parser to extract packages, classes, methods,
signatures, annotations and javadoc. This static pass means we never have to
send raw boilerplate (imports, getters, license headers) to the LLM — only a
compact structural digest — which is the main lever for staying inside token
limits.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional

import javalang

from .code_reader import SourceFile

# Spring/JPA annotations that reveal a class's architectural role.
ROLE_BY_ANNOTATION = {
    "RestController": "controller",
    "Controller": "controller",
    "Service": "service",
    "Repository": "repository",
    "Entity": "entity",
    "Configuration": "configuration",
    "Component": "component",
    "ControllerAdvice": "exception_handler",
    "RestControllerAdvice": "exception_handler",
}

HTTP_MAPPING_ANNOTATIONS = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH", "RequestMapping": "REQUEST",
}

# Decision points counted for cyclomatic complexity.
_DECISION_NODES = (
    javalang.tree.IfStatement,
    javalang.tree.ForStatement,
    javalang.tree.WhileStatement,
    javalang.tree.DoStatement,
    javalang.tree.SwitchStatementCase,
    javalang.tree.CatchClause,
    javalang.tree.TernaryExpression,
)
_BOOL_OPS = re.compile(r"(&&|\|\|)")


@dataclass
class MethodInfo:
    name: str
    signature: str
    return_type: str
    parameters: List[str]
    modifiers: List[str]
    annotations: List[str]
    javadoc: Optional[str]
    http_method: Optional[str]
    http_path: Optional[str]
    cyclomatic_complexity: int
    description: Optional[str] = None  # filled by the LLM stage


@dataclass
class ClassInfo:
    file_path: str
    package: str
    name: str
    kind: str                      # class | interface | enum
    role: str                      # controller | service | repository | ...
    annotations: List[str]
    extends: Optional[str]
    implements: List[str]
    base_path: Optional[str]       # class-level @RequestMapping path
    javadoc: Optional[str]
    loc: int
    methods: List[MethodInfo] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)  # injected field types
    summary: Optional[str] = None  # filled by the LLM stage


def _annotation_names(node) -> List[str]:
    return [a.name for a in (getattr(node, "annotations", None) or [])]


def _annotation_value(node, names) -> Optional[str]:
    """Extract the string value/path of an annotation such as @GetMapping."""
    for a in getattr(node, "annotations", None) or []:
        if a.name not in names:
            continue
        el = a.element
        candidates = []
        if isinstance(el, list):
            candidates = [pair.value for pair in el
                          if getattr(pair, "name", "") in ("value", "path")]
        elif el is not None:
            candidates = [el]
        for c in candidates:
            if isinstance(c, javalang.tree.Literal):
                return c.value.strip('"')
    return None


def _format_type(t) -> str:
    if t is None:
        return "void"
    name = t.name
    args = getattr(t, "arguments", None)
    if args:
        inner = ", ".join(_format_type(a.type) if a.type else "?" for a in args)
        name = f"{name}<{inner}>"
    dims = "[]" * len(getattr(t, "dimensions", []) or [])
    return name + dims


def _cyclomatic(method_node, source_slice: str) -> int:
    cc = 1
    for _, node in method_node.filter(javalang.tree.Node):
        if isinstance(node, _DECISION_NODES):
            cc += 1
    cc += len(_BOOL_OPS.findall(source_slice))
    return cc


def _method_source(content: str, position) -> str:
    """Rough method body slice (from declaration line to brace balance)."""
    if position is None:
        return ""
    lines = content.splitlines()
    start = position.line - 1
    depth = 0
    opened = False
    out = []
    for line in lines[start:]:
        out.append(line)
        depth += line.count("{") - line.count("}")
        if "{" in line:
            opened = True
        if opened and depth <= 0:
            break
    return "\n".join(out)


def parse_java_file(src: SourceFile) -> List[ClassInfo]:
    try:
        tree = javalang.parse.parse(src.content)
    except (javalang.parser.JavaSyntaxError, javalang.tokenizer.LexerError, IndexError):
        return []

    package = tree.package.name if tree.package else ""
    classes: List[ClassInfo] = []

    for _, type_decl in tree.filter(javalang.tree.TypeDeclaration):
        if not isinstance(type_decl, (javalang.tree.ClassDeclaration,
                                      javalang.tree.InterfaceDeclaration,
                                      javalang.tree.EnumDeclaration)):
            continue
        kind = ("interface" if isinstance(type_decl, javalang.tree.InterfaceDeclaration)
                else "enum" if isinstance(type_decl, javalang.tree.EnumDeclaration)
                else "class")
        annos = _annotation_names(type_decl)
        role = next((ROLE_BY_ANNOTATION[a] for a in annos if a in ROLE_BY_ANNOTATION), None)
        if role is None:
            n = type_decl.name
            role = ("repository" if n.endswith("Repository") or n.endswith("RepositoryImpl")
                    else "dto" if "Dto" in n
                    else "mapper" if n.endswith("Mapper")
                    else "assembler" if n.endswith("Assembler")
                    else "converter" if n.endswith("Converter")
                    else "security" if "Security" in n or "Auth" in n or "Jwt" in n or "Token" in n
                    else "exception" if "Exception" in n
                    else "util" if n.endswith("Util") or n.endswith("Utils")
                    else "model")

        extends = None
        if getattr(type_decl, "extends", None) is not None:
            e = type_decl.extends
            extends = ", ".join(_format_type(x) for x in e) if isinstance(e, list) else _format_type(e)
        implements = [_format_type(i) for i in (getattr(type_decl, "implements", None) or [])]

        cls = ClassInfo(
            file_path=src.path, package=package, name=type_decl.name, kind=kind,
            role=role, annotations=annos, extends=extends, implements=implements,
            base_path=_annotation_value(type_decl, {"RequestMapping"}),
            javadoc=(type_decl.documentation or "").strip() or None,
            loc=src.loc,
        )

        for f in getattr(type_decl, "fields", None) or []:
            mods = f.modifiers or set()
            if "final" in mods or "Autowired" in _annotation_names(f):
                cls.dependencies.append(_format_type(f.type))

        for m in getattr(type_decl, "methods", None) or []:
            params = [f"{_format_type(p.type)} {p.name}" for p in m.parameters]
            ret = _format_type(m.return_type)
            mods = sorted(m.modifiers or [])
            sig = f"{' '.join(mods)} {ret} {m.name}({', '.join(params)})".strip()
            m_annos = _annotation_names(m)
            http = next((v for k, v in HTTP_MAPPING_ANNOTATIONS.items() if k in m_annos), None)
            http_path = _annotation_value(m, set(HTTP_MAPPING_ANNOTATIONS)) if http else None
            body_src = _method_source(src.content, m.position)
            cls.methods.append(MethodInfo(
                name=m.name, signature=sig, return_type=ret, parameters=params,
                modifiers=mods, annotations=m_annos, javadoc=(m.documentation or "").strip() or None,
                http_method=http, http_path=http_path,
                cyclomatic_complexity=_cyclomatic(m, body_src),
            ))
        classes.append(cls)
    return classes
