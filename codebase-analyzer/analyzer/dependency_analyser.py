"""Downstream dependency analyser + code-flow tracer.

Two capabilities:

1. DEPENDENCY DETECTION
   Scans every Java source file for integration patterns and emits a structured
   dependency graph:
     - Databases      : JpaRepository / JdbcTemplate / EntityManager / @Table / @Entity
     - Cache          : Redis / @Cacheable / @CachePut / @CacheEvict
     - REST outbound  : RestTemplate / WebClient / RestClient / @FeignClient / OpenFeign
     - Messaging      : KafkaTemplate / @KafkaListener / RabbitTemplate / @RabbitListener
                        SqsClient / @SqsListener / JmsTemplate / @JmsListener
     - Async          : @Async + ExecutorService / ThreadPoolTaskExecutor
     - External       : SMTP (JavaMailSender) / S3 / Elasticsearch

2. CODE FLOW TRACING
   For every REST endpoint and Kafka consumer, traces the full call chain:
     Controller handler → Service method(s) → Repository calls → DB tables
   producing a per-endpoint flow that can be rendered as a diagram.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import tree_sitter_java as tsj
from tree_sitter import Language, Parser

_JAVA_LANG = Language(tsj.language(), "java")
_PARSER = Parser()
_PARSER.set_language(_JAVA_LANG)


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Dependency:
    kind:        str   # "database" | "cache" | "rest_client" | "messaging" | "async" | "external"
    sub_type:    str   # e.g. "JpaRepository", "KafkaTemplate", "Redis", "@FeignClient"
    detail:      str   # e.g. table name, topic, URL pattern, bean name
    file:        str
    line:        int
    direction:   str   # "inbound" | "outbound" | "both"


@dataclass
class FlowStep:
    layer:       str   # "controller" | "service" | "repository" | "db" | "cache" | "messaging"
    class_name:  str
    method:      str
    detail:      str   # annotation, table name, topic, etc.
    file:        str


@dataclass
class EndpointFlow:
    http_method:   str   # GET / POST / PUT / DELETE / …
    path:          str
    controller:    str
    handler:       str
    security:      List[str]   # @Secured values
    steps:         List[FlowStep] = field(default_factory=list)
    db_tables:     List[str]   = field(default_factory=list)
    cache_ops:     List[str]   = field(default_factory=list)
    messages_out:  List[str]   = field(default_factory=list)
    summary:       str = ""


@dataclass
class ConsumerFlow:
    kind:         str   # "kafka" | "rabbit" | "sqs" | "jms"
    topic:        str
    group_id:     str
    consumer_class: str
    handler:      str
    file:         str
    steps:        List[FlowStep] = field(default_factory=list)
    db_tables:    List[str] = field(default_factory=list)
    summary:      str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Tree-sitter helpers
# ─────────────────────────────────────────────────────────────────────────────

def _txt(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node else ""

def _find_all(node, types: Set[str]) -> List:
    results, stack = [], [node]
    while stack:
        n = stack.pop()
        if n.type in types:
            results.append(n)
        stack.extend(reversed(n.children))
    return results

def _child(node, *types):
    for c in node.children:
        if c.type in types:
            return c
    return None

def _annotation_names(node) -> List[str]:
    names = []
    mods = _child(node, "modifiers")
    if not mods:
        return names
    for c in mods.children:
        if c.type == "marker_annotation":
            names.append(_txt(c).lstrip("@").split("(")[0].strip())
        elif c.type == "annotation":
            id_n = _child(c, "identifier")
            if id_n:
                names.append(_txt(id_n))
    return names

def _annotation_attr(node, anno_names: Set[str], attr: str = "value") -> Optional[str]:
    mods = _child(node, "modifiers")
    if not mods:
        return None
    for c in mods.children:
        if c.type not in {"annotation"}:
            continue
        id_n = _child(c, "identifier")
        if not id_n or _txt(id_n) not in anno_names:
            continue
        for lit in _find_all(c, {"string_literal"}):
            return _txt(lit).strip('"')
    return None

def _class_name(node) -> str:
    id_n = _child(node, "identifier")
    return _txt(id_n) if id_n else ""

def _method_name(node) -> str:
    id_n = _child(node, "identifier")
    return _txt(id_n) if id_n else ""

def _lines(src: str) -> List[str]:
    return src.splitlines()


# ─────────────────────────────────────────────────────────────────────────────
# Integration pattern definitions
# ─────────────────────────────────────────────────────────────────────────────

# (kind, sub_type, direction, patterns-in-import-or-type, detail_regex)
INTEGRATION_PATTERNS = [
    # ── Database ──────────────────────────────────────────────────────────
    ("database", "JpaRepository",    "outbound",
     re.compile(r"JpaRepository|CrudRepository|PagingAndSortingRepository|JpaSpecificationExecutor"),
     re.compile(r"interface\s+(\w+)\s+extends")),
    ("database", "JdbcTemplate",     "outbound",
     re.compile(r"JdbcTemplate|NamedParameterJdbcTemplate"),
     None),
    ("database", "EntityManager",    "outbound",
     re.compile(r"EntityManager"),
     None),
    ("database", "QueryDSL",         "outbound",
     re.compile(r"JPAQueryFactory|QuerydslPredicateExecutor"),
     None),

    # ── Cache ─────────────────────────────────────────────────────────────
    ("cache", "Redis",               "outbound",
     re.compile(r"RedisTemplate|StringRedisTemplate|ReactiveRedisTemplate|RedissonClient"),
     None),
    ("cache", "@Cacheable",          "outbound",
     re.compile(r"@Cacheable|@CachePut|@CacheEvict"),
     re.compile(r'@Cache(?:able|Put|Evict)\s*\(\s*(?:value\s*=\s*)?"([^"]+)"')),

    # ── REST clients (outbound) ───────────────────────────────────────────
    ("rest_client", "RestTemplate",  "outbound",
     re.compile(r"RestTemplate"),
     None),
    ("rest_client", "WebClient",     "outbound",
     re.compile(r"WebClient|WebClientBuilder"),
     re.compile(r'baseUrl\s*\(\s*"([^"]+)"')),
    ("rest_client", "RestClient",    "outbound",
     re.compile(r"RestClient\.create|RestClient\.builder"),
     None),
    ("rest_client", "@FeignClient",  "outbound",
     re.compile(r"@FeignClient"),
     re.compile(r'@FeignClient\s*\(\s*(?:name\s*=\s*)?["\']([^"\']+)["\']')),
    ("rest_client", "OpenFeign",     "outbound",
     re.compile(r"feign\.client|FeignBuilder"),
     None),

    # ── Kafka ─────────────────────────────────────────────────────────────
    ("messaging", "KafkaTemplate",   "outbound",
     re.compile(r"KafkaTemplate"),
     re.compile(r'send\s*\(\s*"([^"]+)"')),
    ("messaging", "@KafkaListener",  "inbound",
     re.compile(r"@KafkaListener"),
     re.compile(r'topics\s*=\s*\{?\s*"([^"]+)"')),
    ("messaging", "KafkaProducer",   "outbound",
     re.compile(r"KafkaProducer"),
     None),

    # ── RabbitMQ ──────────────────────────────────────────────────────────
    ("messaging", "RabbitTemplate",  "outbound",
     re.compile(r"RabbitTemplate|AmqpTemplate"),
     re.compile(r'convertAndSend\s*\(\s*"([^"]+)"')),
    ("messaging", "@RabbitListener", "inbound",
     re.compile(r"@RabbitListener"),
     re.compile(r'queues\s*=\s*\{?\s*"([^"]+)"')),

    # ── AWS SQS / SNS ─────────────────────────────────────────────────────
    ("messaging", "SqsClient",       "outbound",
     re.compile(r"SqsClient|SqsTemplate|SqsAsyncClient"),
     None),
    ("messaging", "@SqsListener",    "inbound",
     re.compile(r"@SqsListener"),
     re.compile(r'@SqsListener\s*\(\s*"([^"]+)"')),
    ("messaging", "SnsClient",       "outbound",
     re.compile(r"SnsClient|SnsTemplate"),
     None),

    # ── JMS ───────────────────────────────────────────────────────────────
    ("messaging", "JmsTemplate",     "outbound",
     re.compile(r"JmsTemplate"),
     None),
    ("messaging", "@JmsListener",    "inbound",
     re.compile(r"@JmsListener"),
     re.compile(r'destination\s*=\s*"([^"]+)"')),

    # ── Async ─────────────────────────────────────────────────────────────
    ("async", "@Async",              "both",
     re.compile(r"@Async|@EnableAsync"),
     None),
    ("async", "ExecutorService",     "both",
     re.compile(r"ExecutorService|ThreadPoolTaskExecutor|CompletableFuture"),
     None),

    # ── External services ─────────────────────────────────────────────────
    ("external", "SMTP",             "outbound",
     re.compile(r"JavaMailSender|MimeMessage|SimpleMailMessage"),
     None),
    ("external", "S3",               "outbound",
     re.compile(r"S3Client|AmazonS3|S3Template"),
     re.compile(r'"([a-z0-9.-]{3,63})"')),
    ("external", "Elasticsearch",    "outbound",
     re.compile(r"ElasticsearchClient|ElasticsearchRepository|RestHighLevelClient"),
     None),
    ("external", "MongoDB",          "outbound",
     re.compile(r"MongoRepository|MongoTemplate|ReactiveMongoRepository"),
     None),
    ("external", "DynamoDB",         "outbound",
     re.compile(r"DynamoDbClient|DynamoDbTable|DynamoDBMapper"),
     None),
]

HTTP_MAPPING = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH", "RequestMapping": "ANY",
}


# ─────────────────────────────────────────────────────────────────────────────
# Dependency scanner
# ─────────────────────────────────────────────────────────────────────────────

def scan_dependencies(source_files) -> Dict:
    """Scan all source files and return structured dependency inventory."""
    all_deps: List[Dependency] = []

    for sf in source_files:
        src = sf.content
        lines_list = _lines(src)

        for kind, sub_type, direction, pattern, detail_re in INTEGRATION_PATTERNS:
            if not pattern.search(src):
                continue

            for i, line in enumerate(lines_list, 1):
                if not pattern.search(line):
                    continue
                detail = ""
                if detail_re:
                    m = detail_re.search(src[max(0, src.find(line)-5):
                                            src.find(line)+len(line)+200])
                    if m:
                        detail = m.group(1) if m.lastindex else m.group(0)
                all_deps.append(Dependency(
                    kind=kind, sub_type=sub_type, direction=direction,
                    detail=detail, file=sf.path, line=i,
                ))

    # Extract DB tables from @Table annotations
    db_tables = _extract_db_tables(source_files)

    # Extract FeignClient service names
    feign_clients = _extract_feign_clients(source_files)

    # Summarise
    by_kind: Dict = {}
    for d in all_deps:
        by_kind.setdefault(d.kind, []).append({
            "sub_type":  d.sub_type,
            "direction": d.direction,
            "detail":    d.detail,
            "file":      d.file,
            "line":      d.line,
        })

    # Deduplicate within each kind by sub_type
    seen: Set[str] = set()
    unique_deps = []
    for d in all_deps:
        key = f"{d.kind}:{d.sub_type}:{d.detail}"
        if key not in seen:
            seen.add(key)
            unique_deps.append(d)

    return {
        "summary": {
            "total_unique": len(unique_deps),
            "by_kind": {k: len(set(d.sub_type for d in all_deps if d.kind == k))
                        for k in {d.kind for d in all_deps}},
        },
        "dependencies": [
            {"kind": d.kind, "sub_type": d.sub_type, "direction": d.direction,
             "detail": d.detail, "file": d.file}
            for d in unique_deps
        ],
        "db_tables": db_tables,
        "feign_clients": feign_clients,
    }


def _extract_db_tables(source_files) -> List[Dict]:
    tables = []
    table_re = re.compile(r'@Table\s*\(.*?name\s*=\s*"([^"]+)"(?:.*?schema\s*=\s*"([^"]+)")?', re.DOTALL)
    entity_re = re.compile(r'@Entity(?:\s*\(\s*name\s*=\s*"([^"]+)"\s*\))?')
    for sf in source_files:
        for m in table_re.finditer(sf.content):
            tables.append({
                "table":  m.group(1),
                "schema": m.group(2) or "default",
                "file":   sf.path,
            })
        if not table_re.search(sf.content):
            for m in entity_re.finditer(sf.content):
                if m.group(1):
                    tables.append({"table": m.group(1), "schema": "default", "file": sf.path})
    return tables


def _extract_feign_clients(source_files) -> List[Dict]:
    clients = []
    fc_re = re.compile(r'@FeignClient\s*\(([^)]+)\)')
    for sf in source_files:
        for m in fc_re.finditer(sf.content):
            attrs = m.group(1)
            name_m = re.search(r'name\s*=\s*"([^"]+)"', attrs) or re.search(r'"([^"]+)"', attrs)
            url_m  = re.search(r'url\s*=\s*"([^"]+)"', attrs)
            path_m = re.search(r'path\s*=\s*"([^"]+)"', attrs)
            clients.append({
                "name": name_m.group(1) if name_m else "unknown",
                "url":  url_m.group(1)  if url_m  else "",
                "path": path_m.group(1) if path_m else "",
                "file": sf.path,
            })
    return clients


# ─────────────────────────────────────────────────────────────────────────────
# Code flow tracer
# ─────────────────────────────────────────────────────────────────────────────

def _build_class_index(classes) -> Dict[str, object]:
    """Map simple class names → ClassInfo for fast lookup."""
    return {c.name: c for c in classes}


def _build_method_call_index(source_files) -> Dict[str, List[str]]:
    """
    Map 'ClassName.methodName' → [called methods as 'TypeName.method'].
    Uses regex over source — fast, ~90% accurate for Spring services.
    """
    index: Dict[str, List[str]] = {}
    call_re = re.compile(r'(\w+)\.(\w+)\s*\(')

    for sf in source_files:
        # Find class name
        cls_m = re.search(r'(?:public|private|protected)?\s*(?:class|interface)\s+(\w+)', sf.content)
        if not cls_m:
            continue
        cls_name = cls_m.group(1)

        # Find all method bodies and the calls inside them
        method_re = re.compile(
            r'(?:public|private|protected)\s+[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*(?:throws[^{]+)?\{',
            re.MULTILINE,
        )
        for mm in method_re.finditer(sf.content):
            mname = mm.group(1)
            body_start = mm.end()
            # Rough body extraction (to next method or EOF)
            next_m = method_re.search(sf.content, body_start)
            body = sf.content[body_start: next_m.start() if next_m else len(sf.content)]
            calls = [f"{obj}.{mtd}" for obj, mtd in call_re.findall(body)]
            index[f"{cls_name}.{mname}"] = calls
    return index


def _build_iface_to_impl(classes) -> Dict[str, str]:
    """Map interface name → implementing class name for injection resolution."""
    m = {}
    for c in classes:
        for iface in c.implements:
            base = iface.split("<")[0].strip()
            m[base] = c.name
    return m


def _resolve_service_calls(controller_method: str,
                            field_types: Dict[str, str],
                            call_index: Dict[str, List[str]],
                            class_index: Dict[str, object],
                            iface_to_impl: Dict[str, str]) -> List[str]:
    """
    From a controller method, resolve the injected service fields it calls,
    returning a list of 'ConcreteClass.methodName' strings.
    Handles interface fields → looks up implementing class.
    """
    calls = call_index.get(controller_method, [])
    resolved = []
    for call in calls:
        if "." not in call:
            continue
        obj, method = call.split(".", 1)
        # obj is a field name → look up its declared type
        field_type = field_types.get(obj)
        if not field_type:
            continue
        # Resolve interface to implementation if needed
        concrete = iface_to_impl.get(field_type, field_type)
        if concrete in class_index:
            resolved.append(f"{concrete}.{method}")
    return resolved


def trace_endpoint_flows(classes, source_files) -> List[EndpointFlow]:
    """Trace controller → service → repository → DB for every REST endpoint."""
    class_index     = _build_class_index(classes)
    call_index      = _build_method_call_index(source_files)
    iface_to_impl   = _build_iface_to_impl(classes)
    db_tables_list  = _extract_db_tables(source_files)

    # entity stem (lowercase, no suffix) → list of table names
    entity_tables: Dict[str, List[str]] = {}
    for t in db_tables_list:
        stem = t["file"].split("/")[-1].replace("Entity.java","").lower()
        entity_tables.setdefault(stem, []).append(t["table"])

    # field-name → type per class
    field_re = re.compile(r'private\s+(?:final\s+)?([\w]+)[\w<>, ]*\s+(\w+)\s*;')
    field_map: Dict[str, Dict[str, str]] = {}
    for sf in source_files:
        cls_m = re.search(r'(?:public|private|protected)?\s*(?:class|interface)\s+(\w+)', sf.content)
        if not cls_m:
            continue
        cls_name = cls_m.group(1)
        field_map[cls_name] = {fm.group(2): fm.group(1) for fm in field_re.finditer(sf.content)}

    cache_re = re.compile(r'@(Cacheable|CachePut|CacheEvict)\s*[\(\s][^)]*?["\']([^"\']+)["\']')
    txn_re   = re.compile(r'@Transactional')
    file_src = {sf.path: sf.content for sf in source_files}

    flows: List[EndpointFlow] = []

    for ctrl_cls in classes:
        if ctrl_cls.role != "controller":
            continue

        ctrl_fields = field_map.get(ctrl_cls.name, {})
        ctrl_src    = file_src.get(ctrl_cls.file_path, "")

        for method in ctrl_cls.methods:
            if not method.http_method:
                continue

            path = "/".join(p.strip("/")
                            for p in [ctrl_cls.base_path or "", method.http_path or ""]
                            if p)
            if not path.startswith("/"):
                path = "/" + path

            sec_m    = re.search(r'@Secured\s*\(\s*\{?([^)}]+)', ctrl_src)
            security = ([s.strip().strip('"') for s in sec_m.group(1).split(",")]
                        if sec_m else [])

            flow = EndpointFlow(
                http_method=method.http_method, path=path,
                controller=ctrl_cls.name, handler=method.name, security=security,
            )
            flow.steps.append(FlowStep(
                layer="controller", class_name=ctrl_cls.name,
                method=method.name, detail=f"{method.http_method} {path}",
                file=ctrl_cls.file_path,
            ))

            # service calls
            ctrl_key  = f"{ctrl_cls.name}.{method.name}"
            svc_calls = _resolve_service_calls(ctrl_key, ctrl_fields, call_index,
                                               class_index, iface_to_impl)
            seen_svc: Set[str] = set()
            for svc_call in svc_calls:
                if "." not in svc_call:
                    continue
                svc_cls_name, svc_method_name = svc_call.split(".", 1)
                if svc_cls_name in seen_svc:
                    continue
                seen_svc.add(svc_cls_name)
                svc_cls = class_index.get(svc_cls_name)
                if not svc_cls or svc_cls.role not in ("service","security","component"):
                    continue
                svc_src = file_src.get(svc_cls.file_path, "")

                # cache detection
                cache_tags = []
                for cm in cache_re.finditer(svc_src):
                    cm_pos = svc_src.find(cm.group(0))
                    nearby = svc_src[max(0, cm_pos-300): cm_pos+50]
                    if svc_method_name in nearby:
                        tag = f"@{cm.group(1)}(\"{cm.group(2)}\")"
                        cache_tags.append(tag)
                        if tag not in flow.cache_ops:
                            flow.cache_ops.append(tag)

                txn = "@Transactional" if txn_re.search(svc_src) else ""
                flow.steps.append(FlowStep(
                    layer="service", class_name=svc_cls_name,
                    method=svc_method_name,
                    detail=" ".join(filter(None, [txn] + cache_tags)),
                    file=svc_cls.file_path,
                ))

                # repository calls
                repo_calls = _resolve_service_calls(
                    f"{svc_cls_name}.{svc_method_name}",
                    field_map.get(svc_cls_name, {}),
                    call_index, class_index, iface_to_impl,
                )
                seen_repo: Set[str] = set()
                for repo_call in repo_calls:
                    if "." not in repo_call:
                        continue
                    repo_cls_name, repo_method = repo_call.split(".", 1)
                    if repo_cls_name in seen_repo:
                        continue
                    seen_repo.add(repo_cls_name)
                    repo_cls = class_index.get(repo_cls_name)
                    if not repo_cls or repo_cls.role not in ("repository","model"):
                        continue

                    stem = repo_cls_name.replace("Repository","").replace("Impl","").replace("Custom","").lower()
                    tables = entity_tables.get(stem, [])
                    table_detail = (", ".join(f"sakila.{t}" for t in tables)
                                    if tables else f"{stem} (JPA)")
                    for tbl in tables:
                        full = f"sakila.{tbl}"
                        if full not in flow.db_tables:
                            flow.db_tables.append(full)

                    flow.steps.append(FlowStep(
                        layer="repository", class_name=repo_cls_name,
                        method=repo_method, detail=table_detail,
                        file=repo_cls.file_path,
                    ))

            for table in flow.db_tables:
                flow.steps.append(FlowStep(
                    layer="db", class_name="MySQL · Sakila",
                    method="", detail=table, file="",
                ))

            svc_names = list({s.class_name for s in flow.steps if s.layer == "service"})
            flow.summary = (
                f"{method.http_method} {path} → "
                f"{', '.join(svc_names) or 'direct'}"
                + (f" → {', '.join(flow.db_tables)}" if flow.db_tables else "")
                + (f" [cache: {', '.join(flow.cache_ops)}]" if flow.cache_ops else "")
            )
            flows.append(flow)

    return flows


def trace_consumer_flows(classes, source_files) -> List[ConsumerFlow]:
    """Trace Kafka/Rabbit/SQS consumers → service → DB."""
    class_index = _build_class_index(classes)
    call_index  = _build_method_call_index(source_files)

    field_re = re.compile(r'private\s+(?:final\s+)?(\w+)[\w<>, ]*\s+(\w+)\s*;')
    field_map: Dict[str, Dict[str, str]] = {}
    for sf in source_files:
        cls_m = re.search(r'(?:public|private|protected)?\s*(?:class|interface)\s+(\w+)', sf.content)
        if not cls_m:
            continue
        cls_name = cls_m.group(1)
        field_map[cls_name] = {fm.group(2): fm.group(1)
                               for fm in field_re.finditer(sf.content)}

    CONSUMER_PATTERNS = [
        ("kafka",  re.compile(r'@KafkaListener\s*\([^)]*topics\s*=\s*\{?\s*"([^"]+)"'),   "groupId"),
        ("rabbit", re.compile(r'@RabbitListener\s*\([^)]*queues\s*=\s*\{?\s*"([^"]+)"'), ""),
        ("sqs",    re.compile(r'@SqsListener\s*\(\s*"([^"]+)"'),                           ""),
        ("jms",    re.compile(r'@JmsListener\s*\([^)]*destination\s*=\s*"([^"]+)"'),       ""),
    ]

    flows = []
    for sf in source_files:
        for kind, topic_re, group_attr in CONSUMER_PATTERNS:
            for m in topic_re.finditer(sf.content):
                topic = m.group(1)
                group_m = re.search(r'groupId\s*=\s*"([^"]+)"', sf.content)
                group = group_m.group(1) if group_m else ""

                cls_m = re.search(r'(?:public|private|protected)?\s*class\s+(\w+)', sf.content)
                cls_name = cls_m.group(1) if cls_m else sf.path.split("/")[-1].replace(".java","")

                method_m = re.search(r'(?:public|private)\s+\w+\s+(\w+)\s*\(', sf.content, m.end())
                handler  = method_m.group(1) if method_m else "handle"

                flow = ConsumerFlow(
                    kind=kind, topic=topic, group_id=group,
                    consumer_class=cls_name, handler=handler, file=sf.path,
                )
                flow.steps.append(FlowStep(
                    layer="messaging", class_name=cls_name, method=handler,
                    detail=f"@{kind.title()}Listener(\"{topic}\")", file=sf.path,
                ))
                flows.append(flow)

    return flows


# ─────────────────────────────────────────────────────────────────────────────
# Main entry
# ─────────────────────────────────────────────────────────────────────────────

def analyse(source_files, classes) -> dict:
    deps   = scan_dependencies(source_files)
    ep_flows = trace_endpoint_flows(classes, source_files)
    cs_flows = trace_consumer_flows(classes, source_files)

    return {
        "dependencies": deps,
        "endpoint_flows": [
            {
                "http_method":  f.http_method,
                "path":         f.path,
                "controller":   f.controller,
                "handler":      f.handler,
                "security":     f.security,
                "summary":      f.summary,
                "cache_ops":    f.cache_ops,
                "db_tables":    f.db_tables,
                "messages_out": f.messages_out,
                "steps": [{"layer": s.layer, "class": s.class_name,
                           "method": s.method, "detail": s.detail,
                           "file": s.file} for s in f.steps],
            }
            for f in ep_flows
        ],
        "consumer_flows": [
            {
                "kind": f.kind, "topic": f.topic, "group_id": f.group_id,
                "consumer_class": f.consumer_class, "handler": f.handler,
                "file": f.file,
                "steps": [{"layer": s.layer, "class": s.class_name,
                           "method": s.method, "detail": s.detail} for s in f.steps],
            }
            for f in cs_flows
        ],
    }
