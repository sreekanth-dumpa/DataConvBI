"""Load SKILL.md definitions (frontmatter + system prompt + output schema)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..canonical import sha256_hex

SKILLS_DIR = Path(__file__).parent / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    version: str
    phase: str
    effort: str
    authority: str
    may: tuple[str, ...]
    may_not: tuple[str, ...]
    consumers: tuple[str, ...]
    title: str
    description: str
    system_prompt: str
    schema: dict[str, Any] = field(hash=False)
    path: str = ""

    @property
    def context_hash(self) -> str:
        return sha256_hex(self.version + self.system_prompt + json.dumps(self.schema, sort_keys=True))

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "phase": self.phase, "authority": self.authority,
                "may": list(self.may), "may_not": list(self.may_not), "consumers": list(self.consumers),
                "title": self.title, "path": self.path}


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError("SKILL.md requires frontmatter")
    meta: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        try:
            meta[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            meta[key.strip()] = value
    return meta, m.group(2)


def parse_skill(path: Path) -> Skill:
    meta, body = _frontmatter(path.read_text(encoding="utf-8"))
    title = re.search(r"^# (.+)$", body, re.M).group(1).strip()
    desc = body.split("\n## ")[0].split("\n", 2)[-1].strip()
    prompt = re.search(r"## System prompt\n(.*?)\n## Output schema", body, re.S).group(1).strip()
    schema = json.loads(re.search(r"## Output schema\n+```json\n(.*?)\n```", body, re.S).group(1))
    return Skill(name=meta["name"], version=str(meta["version"]), phase=meta["phase"], effort=meta.get("effort", "low"),
                 authority=meta["authority"], may=tuple(meta.get("may", [])), may_not=tuple(meta.get("may_not", [])),
                 consumers=tuple(meta.get("consumers", [])), title=title, description=desc, system_prompt=prompt,
                 schema=schema, path=str(path))


@lru_cache(maxsize=1)
def load_skills() -> dict[str, Skill]:
    skills = {}
    for p in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        s = parse_skill(p)
        if s.name != p.parent.name:
            raise ValueError(f"skill name {s.name} does not match folder {p.parent.name}")
        skills[s.name] = s
    return skills
