"""Agent skills: provider-neutral LLM roles with deterministic guards.

Each skill is a versioned ``skills/<name>/SKILL.md`` (prompt, output JSON
schema, authority boundary). ``AgentRunner`` invokes a skill through a
provider (Claude, Snowflake Cortex adapter, offline deterministic engine, or
replay), validates the output against the schema, applies the skill's guard,
falls back to the offline engine when needed, and records an invocation
receipt (prompt/context/response hashes, provider, model, verdict) linked to
the trace. Agents propose; the compiler, validators and policies decide.
"""

from .registry import Skill, load_skills  # noqa: F401
from .runner import AgentRunner, SkillResult  # noqa: F401
