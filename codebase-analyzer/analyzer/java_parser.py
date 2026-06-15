"""Java parser — tree-sitter-java (Java 17+).

Replaces the former javalang-based parser. tree-sitter handles the full
modern Java grammar: sealed classes, records, switch expressions, pattern
matching, text blocks, var, generic bounds, etc.

Public surface unchanged: parse_java_file(src: SourceFile) -> List[ClassInfo]
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set

import tree_sitter_java as tsj
from tree_sitter import Language, Parser

# ---------------------------------------------------------------------------
# Parser singleton
# ---------------------------------------------------------------------------
_JAVA_LANG = Language(tsj.language(), "java")
_PARSER = Parser()
_PARSER.set_language(_JAVA_LANG)

# ---------------------------------------------------------------------------
# Domain objects (unchanged API)
# ---------------------------------------------------------------------------
ROLE_BY_ANNOTATION = {
    "RestController": "controller", "Controller": "controller",
    "Service": "service", "Repository": "repository",
    "Entity": "entity", "Configuration": "configuration",
    "Component": "component", "ControllerAdvice": "exception_handler",
    "RestControllerAdvice": "exception_handler",
}

HTTP_MAPPING = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
    "RequestMapping": "REQUEST",
}


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
    description: Optional[str] = None


@dataclass
class ClassInfo:
    file_path: str
    package: str
    name: str
    kind: str          # class | interface | enum | record | sealed_class
    role: str
    annotations: List[str]
    extends: Optional[str]
    implements: List[str]
    base_path: Optional[str]
    javadoc: Optional[str]
    loc: int
    methods: List[MethodInfo] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    summary: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node else ""


def _child_of_type(node, *types):
    for child in node.children:
        if child.type in types:
            return child
    return None


def _children_of_type(node, *types):
    return [c for c in node.children if c.type in types]


def _find_all(node, target_types: Set[str]) -> List:
    """DFS collect all nodes matching any of the given types."""
    results = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in target_types:
            results.append(n)
        stack.extend(reversed(n.children))
    return results


def _annotation_names(node) -> List[str]:
    """Extract bare annotation names from a node's modifiers.
    tree-sitter uses two node types:
      - marker_annotation  → @Foo             (no arguments)
      - annotation         → @Foo(value="x")  (with arguments)
    """
    names = []
    modifiers = _child_of_type(node, "modifiers")
    if not modifiers:
        return names
    for child in modifiers.children:
        if child.type == "marker_annotation":
            # text is b'@Foo' — strip the @
            raw = _text(child)
            if raw.startswith("@"):
                names.append(raw[1:].split("(")[0].strip())
        elif child.type == "annotation":
            name_node = _child_of_type(child, "identifier")
            if name_node:
                names.append(_text(name_node))
    return names


def _annotation_value(node, target_names: Set[str]) -> Optional[str]:
    """Extract the string value/path from a matching annotation."""
    modifiers = _child_of_type(node, "modifiers")
    if not modifiers:
        return None
    for child in modifiers.children:
        if child.type not in {"annotation", "marker_annotation"}:
            continue
        # Get annotation name
        if child.type == "marker_annotation":
            raw = _text(child)
            anno_name = raw.lstrip("@").split("(")[0].strip()
        else:
            name_node = _child_of_type(child, "identifier")
            anno_name = _text(name_node) if name_node else ""
        if anno_name not in target_names:
            continue
        # Extract string value from arguments
        for lit in _find_all(child, {"string_literal"}):
            return _text(lit).strip('"')
    return None


def _modifier_names(node) -> List[str]:
    mods = _child_of_type(node, "modifiers")
    if not mods:
        return []
    return [_text(c) for c in mods.children
            if c.type in {"public","private","protected","static","final",
                          "abstract","synchronized","native","strictfp","default"}]


def _type_text(node) -> str:
    if node is None:
        return "void"
    return _text(node).replace("\n", " ").strip()


def _cyclomatic(method_node) -> int:
    """Cyclomatic complexity = 1 + decision points.
    tree-sitter gives us precise node types for every Java 17 branch form."""
    DECISION_TYPES = {
        "if_statement", "for_statement", "enhanced_for_statement",
        "while_statement", "do_statement",
        "switch_block_statement_group",  # classic switch case
        "switch_rule",                   # switch expression arm (Java 14+)
        "catch_clause",
        "ternary_expression",
        "conditional_expression",        # alias in some grammars
        "binary_expression",             # we filter for && and || below
        "guard_expression",              # pattern guard (Java 21 preview)
    }
    cc = 1
    for node in _find_all(method_node, DECISION_TYPES):
        if node.type == "binary_expression":
            # Only count logical operators
            op_node = _child_of_type(node, "&&", "||")
            if op_node:
                cc += 1
        else:
            cc += 1
    return cc


def _javadoc_before(node) -> Optional[str]:
    """Walk backwards through siblings to find a preceding block_comment."""
    parent = node.parent
    if not parent:
        return None
    prev = None
    for child in parent.children:
        if child == node:
            break
        if child.type in {"block_comment", "line_comment"}:
            prev = child
    if prev and _text(prev).startswith("/**"):
        return _text(prev).strip()
    return None


# ---------------------------------------------------------------------------
# Per-node extractors
# ---------------------------------------------------------------------------

def _extract_method(m_node, src_bytes: bytes, file_path: str) -> Optional[MethodInfo]:
    name_node = _child_of_type(m_node, "identifier")
    if not name_node:
        return None
    name = _text(name_node)

    ret_node = (
        _child_of_type(m_node, "type_identifier", "void_type",
                        "array_type", "generic_type",
                        "integral_type", "floating_point_type",
                        "boolean_type")
        or _child_of_type(m_node, "scoped_type_identifier")
    )
    ret = _type_text(ret_node)

    params_node = _child_of_type(m_node, "formal_parameters")
    params = []
    if params_node:
        for p in _children_of_type(params_node, "formal_parameter",
                                    "spread_parameter"):
            p_type = _child_of_type(p, "type_identifier", "array_type",
                                     "generic_type", "void_type",
                                     "integral_type", "floating_point_type",
                                     "boolean_type", "scoped_type_identifier")
            p_name = _child_of_type(p, "variable_declarator_id", "identifier")
            if p_type and p_name:
                params.append(f"{_type_text(p_type)} {_text(p_name)}")

    mods = _modifier_names(m_node)
    annos = _annotation_names(m_node)
    http = next((v for k, v in HTTP_MAPPING.items() if k in annos), None)
    http_path = _annotation_value(m_node, set(HTTP_MAPPING)) if http else None
    sig = f"{' '.join(mods)} {ret} {name}({', '.join(params)})".strip()

    body = _child_of_type(m_node, "block")
    cc = _cyclomatic(body) if body else 1

    return MethodInfo(
        name=name, signature=sig, return_type=ret, parameters=params,
        modifiers=mods, annotations=annos, javadoc=_javadoc_before(m_node),
        http_method=http, http_path=http_path, cyclomatic_complexity=cc,
    )


def _extract_class(node, src_bytes: bytes, package: str,
                   file_path: str, loc: int) -> Optional[ClassInfo]:
    kind_map = {
        "class_declaration":      "class",
        "interface_declaration":  "interface",
        "enum_declaration":       "enum",
        "record_declaration":     "record",
        "annotation_type_declaration": "annotation",
    }
    kind = kind_map.get(node.type, "class")

    # sealed modifier → mark as sealed_class
    mods = _modifier_names(node)
    if "sealed" in mods:
        kind = "sealed_class"

    name_node = _child_of_type(node, "identifier")
    if not name_node:
        return None
    name = _text(name_node)

    annos = _annotation_names(node)
    role = next((ROLE_BY_ANNOTATION[a] for a in annos if a in ROLE_BY_ANNOTATION), None)
    if not role:
        role = (
            "repository"     if name.endswith(("Repository", "RepositoryImpl")) else
            "dto"            if "Dto" in name or "DTO" in name else
            "mapper"         if name.endswith("Mapper") else
            "assembler"      if name.endswith("Assembler") else
            "converter"      if name.endswith("Converter") else
            "security"       if any(x in name for x in ("Security","Auth","Jwt","Token")) else
            "exception"      if "Exception" in name else
            "util"           if name.endswith(("Util","Utils","Helper")) else
            "record"         if kind == "record" else
            "config"         if "Config" in name else
            "model"
        )

    # extends
    superclass = _child_of_type(node, "superclass")
    extends_str = _type_text(_child_of_type(superclass, "type_identifier",
                                             "generic_type")) if superclass else None

    # implements
    impl_node = _child_of_type(node, "super_interfaces")
    impl_list = []
    if impl_node:
        tl = _child_of_type(impl_node, "interface_type_list", "type_list")
        if tl:
            impl_list = [_type_text(c) for c in tl.children
                         if c.type in {"type_identifier", "generic_type",
                                       "scoped_type_identifier"}]

    base_path = _annotation_value(node, {"RequestMapping"})
    javadoc = _javadoc_before(node)

    # Fields for dependency injection
    deps = []
    body = _child_of_type(node, "class_body", "interface_body",
                           "enum_body", "record_body")
    if body:
        for fd in _find_all(body, {"field_declaration"}):
            f_annos = _annotation_names(fd)
            f_mods = _modifier_names(fd)
            if "final" in f_mods or "Autowired" in f_annos:
                ft = _child_of_type(fd, "type_identifier", "generic_type",
                                     "array_type", "scoped_type_identifier")
                if ft:
                    deps.append(_type_text(ft))

    # Methods
    methods = []
    if body:
        for mn in _children_of_type(body, "method_declaration",
                                     "constructor_declaration"):
            mi = _extract_method(mn, src_bytes, file_path)
            if mi:
                methods.append(mi)

    return ClassInfo(
        file_path=file_path, package=package, name=name, kind=kind, role=role,
        annotations=annos, extends=extends_str, implements=impl_list,
        base_path=base_path, javadoc=javadoc, loc=loc,
        methods=methods, dependencies=deps,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_java_file(src) -> List[ClassInfo]:
    """Parse a SourceFile and return ClassInfo objects.
    Skips files that tree-sitter cannot parse (returns []).
    """
    try:
        src_bytes = src.content.encode("utf-8", errors="replace")
        tree = _PARSER.parse(src_bytes)
        root = tree.root_node
        if root.has_error:
            # Partial parse — continue anyway; tree-sitter recovers gracefully
            pass

        # Package
        package = ""
        pkg_node = _child_of_type(root, "package_declaration")
        if pkg_node:
            id_nodes = _find_all(pkg_node, {"identifier", "scoped_identifier"})
            if id_nodes:
                package = _text(id_nodes[-1])

        classes = []
        TOP_LEVEL = {"class_declaration", "interface_declaration",
                     "enum_declaration", "record_declaration",
                     "annotation_type_declaration"}
        for child in root.children:
            if child.type in TOP_LEVEL:
                ci = _extract_class(child, src_bytes, package,
                                    src.path, src.loc)
                if ci:
                    classes.append(ci)
        return classes

    except Exception:
        return []
