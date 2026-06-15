"""Codebase-level complexity and noteworthy-aspect metrics.

Method-level cyclomatic complexity is computed in java_parser; this module
aggregates it into project/module statistics and flags hotspots.
"""
from collections import Counter, defaultdict
from typing import Dict, List

from .java_parser import ClassInfo


def _module_of(cls: ClassInfo) -> str:
    """Derive the logical module from the package, e.g.
    com.example.app.services.catalog.controller -> services.catalog"""
    parts = cls.package.split(".")
    if "services" in parts:
        i = parts.index("services")
        return ".".join(parts[i:i + 2])
    if "common" in parts:
        return "common"
    if "config" in parts:
        return "config"
    return cls.package or "(default)"


def compute_metrics(classes: List[ClassInfo]) -> Dict:
    methods = [m for c in classes for m in c.methods]
    cc_values = [m.cyclomatic_complexity for m in methods] or [0]

    role_counts = Counter(c.role for c in classes)
    module_stats = defaultdict(lambda: {"classes": 0, "methods": 0, "loc": 0})
    seen_files = {}
    for c in classes:
        mod = _module_of(c)
        module_stats[mod]["classes"] += 1
        module_stats[mod]["methods"] += len(c.methods)
        # count file LOC once per file
        if c.file_path not in seen_files or seen_files[c.file_path] != mod:
            module_stats[mod]["loc"] += c.loc
            seen_files[c.file_path] = mod

    hotspots = sorted(
        ({"class": f"{c.package}.{c.name}", "method": m.name,
          "signature": m.signature, "cyclomatic_complexity": m.cyclomatic_complexity,
          "file": c.file_path}
         for c in classes for m in c.methods if m.cyclomatic_complexity >= 5),
        key=lambda x: -x["cyclomatic_complexity"],
    )[:15]

    endpoints = [
        {"http_method": m.http_method,
         "path": "/" + "/".join(p.strip("/") for p in [c.base_path or "", m.http_path or ""] if p),
         "controller": c.name, "handler": m.name}
        for c in classes for m in c.methods if m.http_method
    ]

    return {
        "totals": {
            "files": len({c.file_path for c in classes}),
            "classes": sum(1 for c in classes if c.kind == "class"),
            "interfaces": sum(1 for c in classes if c.kind == "interface"),
            "enums": sum(1 for c in classes if c.kind == "enum"),
            "methods": len(methods),
            "lines_of_code": sum({c.file_path: c.loc for c in classes}.values()),
            "rest_endpoints": len(endpoints),
        },
        "class_roles": dict(role_counts.most_common()),
        "modules": {k: dict(v) for k, v in sorted(module_stats.items())},
        "cyclomatic_complexity": {
            "average": round(sum(cc_values) / len(cc_values), 2),
            "max": max(cc_values),
            "methods_over_10": sum(1 for v in cc_values if v > 10),
            "distribution": {
                "simple_1_4": sum(1 for v in cc_values if v <= 4),
                "moderate_5_10": sum(1 for v in cc_values if 5 <= v <= 10),
                "complex_over_10": sum(1 for v in cc_values if v > 10),
            },
        },
        "complexity_hotspots": hotspots,
        "rest_endpoints": sorted(endpoints, key=lambda e: (e["controller"], e["path"])),
    }


def module_of(cls: ClassInfo) -> str:
    return _module_of(cls)
