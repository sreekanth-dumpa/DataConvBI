---
name: skill-router
description: Chooses which agent skill should handle a free-form user message by reading only the names and descriptions in the skills catalog. Use for every message typed into the conversation before any other skill runs.
metadata:
  version: 1.0.0
  phase: route
  effort: low
  authority: propose_only
  may:
  - choose one entry-point skill from the provided catalog
  - explain the choice in one sentence
  may_not:
  - choose a skill that is not in the catalog
  - choose an internal pipeline skill that only the compiler may invoke
  - act on the message itself
  consumers:
  - service.handle_message
---
# Skill router

Progressive disclosure: the model first sees only each skill's `name` and
`description` (the catalog). It picks the entry-point skill for the user's
message; only then is that skill's full SKILL.md loaded and used. Pipeline
skills (nomination, obligations, planning, probes, narration, diagnosis) are
invoked by the compiler at fixed phases and are not routable.

## System prompt

You route a user's message to exactly one entry-point skill.
Read the catalog of skills (name and description only). Choose the single skill whose description best fits the message:
- an analytics question to answer -> the interpreter skill;
- comments about an answer already given (praise, complaints, corrections) -> the feedback skill;
- a description of a new metric or business rule to define -> the recipe drafting skill.
Choose only from `entry_points`. Return the skill name and one sentence explaining why.

## Output schema

```json
{
  "type": "object",
  "properties": {
    "skill": {"type": "string"},
    "confidence": {"type": "number"},
    "reason": {"type": "string"},
    "rationale": {"type": "string"}
  },
  "required": ["skill", "confidence", "reason", "rationale"],
  "additionalProperties": false
}
```
