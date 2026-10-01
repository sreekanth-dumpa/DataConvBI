# Agent skills

This is the skills folder the system's LLM uses. Each skill is a folder with a `SKILL.md` in the Agent Skills format:

* **YAML frontmatter.** `name` and `description` are the standard fields. The description says what the skill does
  and when to use it, and it is the first thing the LLM reads. A `metadata` block adds version, phase, effort,
  authority, may / may not, and consumers.
* **Body.** An overview, the `## System prompt` sent to the model, and the `## Output schema`. The model's JSON
  output must satisfy the schema. With Claude, the schema is passed as structured-output `json_schema`.

## How the LLM uses this folder

1. **Discovery.** `ipr.agents.registry.load_skills()` reads every `skills/*/SKILL.md` and validates its frontmatter.
   The `skill-router` skill sees only the catalog (`name` + `description`) and picks the entry-point skill for a
   free-form message (`IPRService.handle_message`).
2. **Use.** `AgentRunner.invoke(name, inputs)` sends that skill's system prompt and output schema to the configured
   provider. The providers are Claude, a Snowflake Cortex adapter, an offline deterministic engine, and replay.
   The runner validates the output against the schema, applies the skill's guard (`ipr/agents/guards.py`), and
   falls back to the offline engine when either check fails. Every call leaves a receipt with prompt, context and
   response hashes.
3. **Authority.** Skills propose; the compiler, validators and policies decide. `may_not` lists what each guard
   enforces.

| Skill | Phase | Entry point? |
|---|---|---|
| `skill-router` | route | — (chooses among entry points) |
| `intent-interpreter` | interpret | yes |
| `feedback-classifier` | improve | yes |
| `recipe-drafter` | build recipe | yes |
| `recipe-nominator` | ground | pipeline |
| `clarification-writer` | clarify / confirm | pipeline |
| `obligation-proposer` | plan | pipeline |
| `plan-proposer` | plan | pipeline |
| `probe-advisor` | plan | pipeline |
| `answer-narrator` | present | pipeline |
| `assessment-diagnostician` | assess | pipeline |

**Adding a skill.** Create `skills/<name>/SKILL.md` in this format, add an offline implementation to
`ipr/agents/offline.py`, and add a guard to `ipr/agents/guards.py`. `tests/test_agents.py` checks the format of
every skill.

**Provider.** Set `IPR_LLM_PROVIDER=claude` with `ANTHROPIC_API_KEY` to use Claude. Without credentials the offline
engine runs every skill. You can also set `IPR_SKILLS_DIR` to load a different skills folder.
