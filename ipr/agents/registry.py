"""Load the agent skills folder (repo-root ``skills/``, Agent Skills format).

Each ``skills/<name>/SKILL.md`` has YAML frontmatter with the standard
``name`` and ``description`` fields (the description says what the skill does
and when to use it; it is what an LLM reads to pick a skill) plus a
``metadata`` block (version, phase, effort, authority, may / may not,
consumers). The body holds the instructions, the system prompt sent to the
model, and the JSON schema the model's output must satisfy.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..canonical import sha256_hex

SKILLS_DIR = Path(os.environ.get("IPR_SKILLS_DIR", Path(__file__).resolve().parents[2] / "skills"))
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    version: str
    phase: str
    effort: str
    authority: str
    may: tuple[str, ...]
    may_not: tuple[str, ...]
    consumers: tuple[str, ...]
    title: str
    overview: str
    system_prompt: str
    schema: dict[str, Any] = field(hash=False)
    path: str = ""

    @property
    def context_hash(self) -> str:
        return sha256_hex(self.version + self.system_prompt + json.dumps(self.schema, sort_keys=True))

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "version": self.version, "phase": self.phase,
                "authority": self.authority, "may": list(self.may), "may_not": list(self.may_not),
                "consumers": list(self.consumers), "title": self.title, "path": self.path}


def validate_frontmatter(meta: dict[str, Any], folder: str) -> list[str]:
    errs = []
    name, desc = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or not NAME_RE.match(name) or len(name) > 64:
        errs.append(f"name {name!r} must be lowercase letters, digits and hyphens (max 64)")
    if name != folder:
        errs.append(f"name {name!r} must equal its folder {folder!r}")
    if not isinstance(desc, str) or not desc.strip():
        errs.append("description is required")
    elif len(desc) > 1024 or re.search(r"<[^>]+>", desc):
        errs.append("description must be at most 1024 characters and contain no XML tags")
    md = meta.get("metadata") or {}
    for k in ("version", "phase", "authority"):
        if k not in md:
            errs.append(f"metadata.{k} is required")
    return errs


def parse_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"{path}: SKILL.md requires YAML frontmatter")
    meta = yaml.safe_load(m.group(1))
    errs = validate_frontmatter(meta, path.parent.name)
    if errs:
        raise ValueError(f"{path}: " + "; ".join(errs))
    body = m.group(2)
    md = meta["metadata"]
    title = re.search(r"^# (.+)$", body, re.M).group(1).strip()
    overview = body.split("\n## ")[0].split("\n", 2)[-1].strip()
    prompt = re.search(r"## System prompt\n(.*?)\n## Output schema", body, re.S).group(1).strip()
    schema = json.loads(re.search(r"## Output schema\n+```json\n(.*?)\n```", body, re.S).group(1))
    return Skill(name=meta["name"], description=meta["description"].strip(), version=str(md["version"]), phase=md["phase"],
                 effort=md.get("effort", "low"), authority=md["authority"], may=tuple(md.get("may", [])),
                 may_not=tuple(md.get("may_not", [])), consumers=tuple(md.get("consumers", [])), title=title, overview=overview,
                 system_prompt=prompt, schema=schema, path=str(path))


@lru_cache(maxsize=4)
def load_skills(skills_dir: str | None = None) -> dict[str, Skill]:
    root = Path(skills_dir) if skills_dir else SKILLS_DIR
    if not root.is_dir():
        raise FileNotFoundError(f"skills folder not found: {root}")
    return {p.parent.name: parse_skill(p) for p in sorted(root.glob("*/SKILL.md"))}


def catalog(skills: dict[str, Skill]) -> list[dict[str, str]]:
    """What an LLM sees first (progressive disclosure): only names and descriptions."""
    return [{"name": s.name, "description": s.description} for s in skills.values()]
