# Agentic Information Path Routing — reference POC

An evidence-bound information compiler above foundational data products. The
POC follows the architecture spec end to end for one certified analytical
algebra: personal auto quote conversion over a configurable window,
population, cohort, cutoff and dimensions. It runs on a synthetic fixture.

```
pip install -r requirements.txt
python -m ipr demo --compare-variants   # full workflow once, plus assessment
streamlit run app.py                    # UI: Ask / Execution / Plan / Cache / Assess / Recipe builder / Monitoring / Trace
python -m pytest                        # oracle, variants, agents, cache safety, recovery, UI
```

## Agent skills (LLM)

The spec's agent roles are eleven versioned skills in `skills/<name>/SKILL.md` at the repo root. Each file holds the
standard `name` and `description` frontmatter, a `metadata` block, the prompt, an output JSON schema and an authority
boundary ("may" / "may not"). See `skills/README.md` for how the LLM discovers and uses them. The skills are
`skill-router`, `intent-interpreter`, `recipe-nominator`, `clarification-writer`, `obligation-proposer`, `plan-proposer`,
`probe-advisor`, `answer-narrator`, `assessment-diagnostician`, `feedback-classifier` and `recipe-drafter`.

`AgentRunner` calls a skill through a provider:

| Provider | When it is used |
|---|---|
| Claude | `IPR_LLM_PROVIDER=claude` plus `ANTHROPIC_API_KEY`. Uses structured JSON output and server-side refusal fallback. |
| Snowflake Cortex | Adapter only; not integration-tested. |
| Offline deterministic engine | The default when no credentials are set. |
| Replay | Returns frozen proposals recorded earlier. |

The runner validates every output against the skill's schema, then applies a deterministic guard. Examples
of what the guards enforce:

* numeric claims must trace to validated facts;
* a plan proposal may not change the certified operator path;
* obligations can only be added, never removed;
* feedback asking to bypass access is flagged and never acted on.

If an output fails either check, the runner falls back to the offline engine and records why. Each call
leaves an invocation receipt with prompt, context and response hashes, plus a trace span. The UI's
**Agent skills** tab shows the catalog, prompts, schemas and receipts, and runs a proposal-stability check.

See `docs/SPEC_COVERAGE.md` for a section-by-section map of the spec to code, tests and status.

The central invariant: the same confirmed meaning, bound parameters, source
cut, access decision and recipe versions produce equivalent information across
admitted physical plans. The tests check this for the fused, staged,
co-partitioned and runtime-adapted plans against an independent oracle and
hand-labeled edge cases.

## Workflow (spec section 2) → code

| Phase | What happens | Code |
|---|---|---|
| Interpret | The `intent-interpreter` skill proposes a draft intent. Writes an interpretation receipt. | `intent.py`, `agents/` |
| Ground | Route by alias or pattern, then hard applicability rules. Semantic search only nominates candidates; they must pass the same rules. Binds recipe definitions, asks material questions, derives **Pass A** obligations and picks evidence by greedy set cover. Reads metadata only. | `grounding.py`, `obligations.py`, `catalog/*.json` |
| Clarify / confirm | Confirmation binds to an RFC 8785 JCS + SHA-256 hash of the typed, normalized semantic payload. Any semantic edit invalidates it. | `canonical.py`, `intent.py`, `service.confirm` |
| Retrieve & resolve | Authorized product ports. Requester-scoped access (ADR-01). Source cut is the last complete publication whose watermark covers the cutoff; the cut fails temporal admission when none does. | `cuts.py`, `access.py` |
| Check reuse | Exact or derived reuse of cached information sets, with every read re-authorized. Saved-plan applicability. | `cache.py`, `service.prepare` |
| Plan | Typed logical IR, then **Pass B** closure over operators, edges, bindings, materializations and outputs. Probes are admitted only when they pay for themselves. Three certified physical variants: fused, staged, and co-partitioned *before* the window reduction. Estimates and a Pareto choice follow. Plans are saved before execution. | `compiler.py`, `sqlgen.py`, `profiling.py`, `rewrite.py`, `cost.py` |
| Execute | Durable admission plus an outbox. Warm workers with fair share, leases, fencing tokens and tagged attempts. Lost acks and expired leases are reconciled before any retry. Shared producers are supported. The unstarted suffix can be adapted at a checkpoint. Cancellation is supported. There are no cron jobs or DAG files. | `runtime/online.py` |
| Validate & cache | Node and final assertions: grain, conservation, numerator ≤ denominator, no future knowledge, half-open window, partition cover and conservation, stage registration. Publication uses compare-and-set and happens only for complete, validated, pinned results. | `validation.py`, `service._h_validate/_h_publish` |
| Present | Answer facts come first. The chart is a deterministic Vega-Lite spec. Exclusions and imbalance are shown per dimension, and confidence is given as categories. | `presentation.py` |
| Assess / improve | A gate vector, with each metric separate and `null` plus a reason when unmeasurable. Writes `assessment.md` and `case.json`. Feedback moves through a lifecycle, approval is advisory-only, and retrieval is scoped. | `assessment.py`, `feedback.py` |
| Build recipe | Six obligation-driven questions. Draft recipes match against the installed algebra and come with adversarial examples. A draft is never certified. | `recipe_builder.py` |
| Monitor / trace | Request, node and cost metrics with availability states. The trace store is hash-linked, verifies against an anchor, and exports spans in an OTel-compatible shape. | `monitoring.py`, `trace.py` |

## Recipes installed

* `gross_submission_quote_conversion@2.0.0` (target, section 4). Counts each submission once across branches. Dimensions come from the first eligible quoted branch, with a certified tie-break. A committed bind on any linked branch converts the submission. The window is `[anchor, anchor + N local days)` in America/New_York, using the zoneinfo DST rule. Gross: cancellation does not reverse conversion. Immature opportunities are excluded and reported.
* `quote_conversion@1.0.0` (legacy, deprecated but still supported). Counts per branch, using UTC dates and an inclusive window. It is only selected when explicitly requested. It has its own semantic hash and is never cost-ranked against v2.

"Certified" here means certified **against the synthetic fixture**: an independent oracle (`ipr/oracle.py`) plus hand labels (`fixture.EDGE_LABELS`). It is not an enterprise Guidewire certification.

## Implementation boundaries (honest scope)

* The data plane is SQLite. It simulates the parts of Snowflake the control plane relies on: requester sessions, `QUERY_TAG`, async submission, query history with a result limit, interrupt-based cancellation, and native row-access policy evaluated at query time. Spill and billing are reported **unavailable**, never zero. "Credits" are simulated credit-equivalents for the Gen1 rates with a 60 s resume floor.
* `engines/snowflake_engine.py` (live adapter) and `runtime/temporal_adapter.py` (ADR-02 target) are design adapters and are **not integration-tested**. The local runtime and control store stand in for Temporal and Aurora.
* Delegated identity is simulated: tokens can expire and the runtime pauses with no service-principal fallback. There is no OAuth integration.
* Profiling probes run as exact queries on the fixture. Live estimates and EXPLAIN-based estimates are not implemented.
* VeriEQL is an adapter that reports `unsupported`. TLP is implemented for projection/filter queries only.
* Heavy-key isolation and promotion of hot intermediates are not compiled. Heavy hitters are measured and reported.
* Execution context lives in memory, so recovery across a *process* restart is limited to durable admission and the outbox (tested). It does not cover handler state.
* Performance numbers come from the fixture and say nothing about scale. Adaptation payoff and plan regret are reported only when they were actually measured.
