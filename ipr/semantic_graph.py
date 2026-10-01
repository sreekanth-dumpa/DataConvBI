"""Versioned semantic graph (section 5): metrics, entities, relationships and
source mappings. The resolver reports supported, clarification-required and
unsupported meanings with the path (or the missing link) that decides it.
Semantic mapping errors are evaluated independently of SQL generation.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

PATH = Path(__file__).parent / "catalog" / "semantic_graph.json"


class SemanticGraph:
    def __init__(self, data: dict[str, Any] | None = None):
        self.data = data or json.loads(PATH.read_text())
        self.version = self.data["registry_version"]

    def attribute_names(self) -> list[str]:
        return sorted(self.data["attributes"])

    def _path(self, src: str, dst: str) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
        """Shortest relationship path from src to dst (either direction); returns (path, uncertified links on it)."""
        adj: dict[str, list[tuple[str, dict]]] = {}
        for r in self.data["relationships"]:
            adj.setdefault(r["from"], []).append((r["to"], r))
            adj.setdefault(r["to"], []).append((r["from"], r))
        seen, q = {src}, deque([(src, [])])
        while q:
            node, path = q.popleft()
            if node == dst:
                return path, [r for r in path if not r["certified"]]
            for nxt, rel in adj.get(node, []):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append((nxt, path + [rel]))
        return None, []

    def resolve_dimension(self, dim: str, recipe: dict[str, Any]) -> dict[str, Any]:
        attr = self.data["attributes"].get(dim)
        metric = self.data["metrics"].get(recipe["id"])
        if attr is None:
            return {"dimension": dim, "status": "unsupported", "reason": f"'{dim}' is not a registered attribute in {self.version}"}
        unit = metric["unit_entity"] if metric else None
        path, uncertified = self._path(attr["entity"], unit) if unit else (None, [])
        if attr["mapping"] is None:
            return {"dimension": dim, "status": "unsupported", "entity": attr["entity"],
                    "reason": f"no certified source mapping for {dim} ({attr['entity']}) in any authorized product port"}
        if path is None:
            return {"dimension": dim, "status": "unsupported", "reason": f"no relationship path from {attr['entity']} to {unit}"}
        if uncertified:
            return {"dimension": dim, "status": "unsupported",
                    "reason": "path uses uncertified relationships: " + ", ".join(f"{r['from']}->{r['to']}" for r in uncertified)}
        if attr["mapping"]["port"] not in recipe["input_ports"]:
            return {"dimension": dim, "status": "unsupported", "reason": f"mapped port {attr['mapping']['port']} is not a recipe input"}
        if dim not in recipe["allowed_dimensions"]:
            return {"dimension": dim, "status": "clarification_required",
                    "reason": f"{dim} is mapped but the recipe does not certify it as an output dimension"}
        return {"dimension": dim, "status": "supported", "entity": attr["entity"], "attribution": attr["attribution"],
                "mapping": attr["mapping"], "path": [f"{r['from']}->{r['to']} ({r['cardinality']})" for r in path]}
