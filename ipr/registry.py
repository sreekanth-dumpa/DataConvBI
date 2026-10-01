"""Versioned registries: recipes, metric templates, operators, obligations,
policies, evidence fragments, discovery packets, products and rewrite rules.

The compiler derives obligations from these registries, not from an agent's
recollection (section 5). Registry versions are recorded in every manifest.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOG_DIR = Path(__file__).parent / "catalog"


def _load(name: str) -> dict[str, Any]:
    with open(CATALOG_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class Registry:
    recipes: list[dict[str, Any]]
    templates: dict[str, Any]
    operators: dict[str, Any]
    obligations: dict[str, Any]
    policies: dict[str, Any]
    budget_policies: dict[str, Any]
    evidence: list[dict[str, Any]]
    packets: dict[str, Any]
    products: dict[str, Any]
    rewrite_rules: dict[str, Any]
    versions: dict[str, str] = field(default_factory=dict)

    # ---- recipes -------------------------------------------------------
    def recipe(self, recipe_id: str, version: str | None = None) -> dict[str, Any]:
        matches = [r for r in self.recipes if r["id"] == recipe_id and (version is None or r["version"] == version)]
        if not matches:
            raise KeyError(f"unknown recipe {recipe_id}@{version}")
        return copy.deepcopy(sorted(matches, key=lambda r: r["version"])[-1])

    def recipe_ref(self, recipe: dict[str, Any]) -> dict[str, str]:
        return {"id": recipe["id"], "version": recipe["version"], "binding_version": recipe["binding_version"],
                "certificate_ref": recipe["certification"]["certificate_ref"]}

    # ---- products ------------------------------------------------------
    def port(self, port_ref: str) -> dict[str, Any]:
        product_id, port_id = port_ref.split("/")
        product = self.products[product_id]
        port = copy.deepcopy(product["ports"][port_id])
        port.update({"product_id": product_id, "port_id": port_id, "authority": product["authority"], "owner": product["owner"]})
        return port

    def evidence_by_id(self) -> dict[str, dict[str, Any]]:
        return {e["claim_id"]: e for e in self.evidence}

    def add_recipe(self, recipe: dict[str, Any]) -> None:
        """Install a recipe (used only by tests / certified publication workflow)."""
        self.recipes.append(recipe)


@lru_cache(maxsize=1)
def _cached() -> Registry:
    recipes = _load("recipes.json")
    templates = _load("metric_templates.json")
    operators = _load("operators.json")
    obligations = _load("obligations.json")
    policies = _load("policies.json")
    evidence = _load("evidence.json")
    packets = _load("packets.json")
    products = _load("products.json")
    rules = _load("rewrite_rules.json")
    versions = {
        "recipes": recipes["registry_version"], "templates": templates["registry_version"],
        "operators": operators["registry_version"], "obligations": obligations["registry_version"],
        "policies": policies["registry_version"], "evidence": evidence["registry_version"],
        "packets": packets["registry_version"], "products": products["registry_version"],
        "rewrite_rules": rules["registry_version"],
    }
    return Registry(
        recipes=recipes["recipes"], templates=templates["templates"], operators=operators["operators"],
        obligations=obligations["obligations"], policies=policies["policies"],
        budget_policies=policies["budget_policies"], evidence=evidence["fragments"],
        packets=packets["families"], products=products["products"], rewrite_rules=rules["rules"], versions=versions,
    )


def load_registry() -> Registry:
    """Return a private deep copy so tests can mutate without leaking state."""
    return copy.deepcopy(_cached())
