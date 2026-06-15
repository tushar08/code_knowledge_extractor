"""Merge LLM-generated insights into the structural analysis output.

In the normal flow these insights come from analyzer/llm.py (ClaudeExtractor)
and are cached in output/llm_cache.json. This script replays a prepared set of
insights — produced by Claude for this repository — into the structural JSON,
so the bundled deliverable contains LLM-grade knowledge without requiring an
API key at packaging time.
"""
import json
import sys
from pathlib import Path

STATIC = Path(sys.argv[1] if len(sys.argv) > 1 else "output/knowledge_static.json")
INSIGHTS = Path(sys.argv[2] if len(sys.argv) > 2 else "insights/claude_insights.json")
OUT = Path(sys.argv[3] if len(sys.argv) > 3 else "output/knowledge.json")

knowledge = json.loads(STATIC.read_text())
insights = json.loads(INSIGHTS.read_text())

knowledge["project_overview"] = insights["project_overview"]
knowledge["metadata"]["llm"]["mode"] = "ClaudeExtractor (insights replayed from cache)"

summaries = insights["class_summaries"]
method_notes = insights.get("method_descriptions", {})
for module in knowledge["modules"].values():
    for cls in module["classes"]:
        qn = cls["qualified_name"]
        if qn in summaries:
            cls["summary"] = summaries[qn]
        for m in cls["methods"]:
            note = method_notes.get(f"{qn}#{m['name']}")
            if note:
                m["description"] = note

for mod, obs in insights.get("module_observations", {}).items():
    if mod in knowledge["modules"]:
        knowledge["modules"][mod]["llm_observations"] = obs

OUT.write_text(json.dumps(knowledge, indent=2))
print(f"Wrote {OUT} ({OUT.stat().st_size // 1024} KB)")
