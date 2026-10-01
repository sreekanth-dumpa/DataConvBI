# Spec coverage matrix

Status key: **Implemented** = working code with tests · **Simulated** = implemented against the local SQLite
stand-in for an enterprise system · **Adapter** = design adapter, not integration-tested · **Boundary** =
deliberately not implemented (reason given).

## Agent / LLM skills (where the spec says "agent")

| Spec phrase | Skill (`ipr/agents/skills/<name>/SKILL.md`) | Guard (what the agent cannot do) | Used in | Tests |
|---|---|---|---|---|
| §2 Interpret: "Agent proposes intent" | `intent-interpreter` | ISO dates, state code, LOB, window bounds; unknown breakdowns stay visible as unsupported | `service.interpret` | test_agents |
| §5 "Use semantic search to locate additional candidate recipes" | `recipe-nominator` | only registered recipe ids; hard applicability rules still decide | `Grounder.candidate_recipes` | test_agents (schema violation fallback) |
| §2 Clarify: "Present recipe-grounded population, definitions…" | `clarification-writer` | same question ids/options; no numbers not in the bound intent; presentation only, never hashed | `service.ground`, UI | test_agents, test_app |
| §5 "The agent can propose extra obligations… cannot remove, waive" | `obligation-proposer` | registry ids only; removals recorded and rejected | `service.plan` | test_agents::test_obligation_removal_is_rejected |
| §8 `agent_propose_using_approved_operators`; §7 "agent selects an approved operator ID and parameters" | `plan-proposer` | approved operators only; operator path must equal the certified composition (else `semantic_delta`); variant preference honoured only inside the Pareto frontier; Python operators by id + bounded params | `service.plan` | test_agents (3 tests), test_operators_graphs |
| §6 probe "expected decision benefit" | `probe-advisor` | approved probe names only; budget broker still admits | `Profiler.collect` | test_temporal_budget |
| §15 "Generate prose and charts from those facts… agent can recommend alternatives, validated" | `answer-narrator` | every number must trace to validated facts; claim refs resolve and match; no causal language; chart fields must be output dims | `_h_present`, `reuse_answer` | test_agents (hallucinated number, causal claim) |
| §17 "An assessment agent may diagnose a mismatch but cannot redefine the gold answer" | `assessment-diagnostician` | evidence must cite metrics in the assessment; no bypass recommendations; gates/metrics not writable | `service.assess` | test_agents |
| §16/§17 change classification; "User feedback remains a data item" | `feedback-classifier` | bypass requests flagged, never actioned | `service.assess` | test_agents::test_feedback_bypass… |
| §18 / §21 "optional LLM performs… recipe questions" | `recipe-drafter` | options must be offered; undecided questions stay unresolved; drafts never certified | `draft_recipe_answers`, UI | test_agents, test_app |
| §3 provider-neutral interface; Cortex optional | `providers.py`: Claude (structured JSON output, server-side refusal fallback), Cortex (**Adapter**), Offline deterministic engine, Replay | schema validation + guard + fallback to offline | all skills | test_agents (fake SDK client) |
| §7 "Store prompt, context and response hashes plus provider/model versions"; "replay without asking the model to reinterpret" | `agent_invocations` table, `ReplayProvider`, trace spans `agent:<skill>` | — | runner | test_agents::test_replay… |
| §21.1 inference stability under concurrency | `agents/stability.py` | — | UI Skills tab | test_agents::test_stability_harness |
| §14 "Agent reasoning summaries should be short, auditable" | `rationale` field (≤280 chars in trace) | — | every skill | — |

No API key is configured in this environment, so the offline engine is the default provider; `IPR_LLM_PROVIDER=claude` (with
`ANTHROPIC_API_KEY`) switches every skill to Claude with the same schemas and guards.

## Sections

| § | Requirement | Where | Status |
|---|---|---|---|
| 1 | Central invariant: equivalent information across admitted plans | `rewrite.compare_results`, `service.assess(compare_variants)`, `shadow_evaluate` | Implemented (fixture) |
| 2 | 14-phase workflow with durable records and admission rules | `service.py` phases; UI tabs | Implemented |
| 2 | Preferences authorize later admissions within an envelope; execute or queue | `set_preferences`, `admission_decision`, `execute(defer_until)` | Implemented |
| 3 | Presentation/control/data planes; online runtime; warm workers | `app.py` / `service.py`+`store.py` / `engines/` ; `runtime/online.py` | Implemented (local) |
| 3.1 | ADR-01 delegated identity, per-principal pools, no service fallback, reuse only on certified access class, reauth before every read, policy change aborts remaining stages, unknown deps forbid class | `access.py`, `engines/sqlite_engine.py`, `_node_admission`, `cache.lookup`, `page_result` | Simulated (no OAuth) |
| 4 | Intent record template, constraints, clarifications, confirmation | `intent.py` | Implemented |
| 4.1 | Submission identity across branches, first-branch attribution, maturity counts per dimension, typed-normalized JCS hash | `sqlgen.cohort_sql`, `presentation.py`, `canonical.py` | Implemented |
| 5 | Two-pass compiler-derived obligations, closure, agent add-only | `obligations.py` | Implemented |
| 5 | 20 packet families, evidence fragments, set-cover selection, evidence manifest fields | `catalog/packets.json`, `catalog/evidence.json`, `Grounder.select_evidence` | Implemented (5 fixture fragments) |
| 5 | Versioned semantic graph; supported / clarification / unsupported with reasons | `catalog/semantic_graph.json`, `semantic_graph.py` | Implemented |
| 6 | Profiles with cut, scope, time, exact/approx label; reuse before probing; decision-benefit probes; budget stop | `profiling.py` (6 probes incl. linkage coverage, key-aware sample) | Implemented |
| 6 | Metadata-only EXPLAIN as planning info | `service.plan` → `explain`, `insights.py` | Implemented (SQLite EXPLAIN QUERY PLAN) |
| 7 | Logical IR vs physical plan; approved operators; AST validation; binds; allowlist; acyclic/complete graph | `compiler.py`, `sqlgen.py` | Implemented |
| 7 | Approved Python operators pinned by version/digest/determinism | `pyoperators.py`, `_h_python`, `a.python_operator_digest` | Implemented |
| 7.1 | Rewrite evidence classes; bounded verifier adapter; unknown never promotes | `rewrite.py`, `catalog/rewrite_rules.json` | Implemented; VeriEQL **Adapter** (reports unsupported) |
| 8 | Routing protocol with explicit gaps and reconfirmation loops | `service.py` (`Gap` kinds) | Implemented |
| 8 | Pareto frontier; constraints not weights | `cost.choose` | Implemented |
| 8 | Bao-inspired learned selection, rule-based until enough receipts, probability recorded | `selector.py` | Implemented |
| 8.1 | Resume floor, reservations before every stage, statement timeout, whole-run deadline, probe downgrade | `cost.BudgetBroker`, runtime timeout/deadline | Simulated credits |
| 9 | Grain obligations, envelope comparison after each materialized join, semijoin key reduction | `validation.py`, runtime envelopes, `linked_binds_sql` | Implemented |
| 9 | Heavy-key isolation / salting | — | Boundary: heavy hitters are measured and reported; isolation not compiled |
| 10.1 | SQL decomposition DAGs composed into an information-path DAG | `compiler.annotate_child_graphs`, `child_graphs` | Implemented |
| 10.2 | Persistent plan registry objects; immutable content-addressed artifacts | `store.py` tables, `artifacts/plans/*.json` | Implemented |
| 10.3 | Durable admission + outbox, stable workflow id, async submission, status polling | `runtime/online.py` | Implemented |
| 10.4 | Node states, fair share, critical-path priority, shared producers, suffix-only adaptation (POP-style validity range, envelope trigger, revision cap) | `runtime/online.py`, `service._adaptation_policy` | Implemented |
| 10.5 | Pre-persisted tagged attempts, submission-unknown, tag-history reconciliation with completeness, fencing, no blind resubmission | `runtime/online.py::_reconcile` | Implemented |
| 10.6 | ADR-02 Temporal + Aurora | `runtime/temporal_adapter.py`; local store/runtime | **Adapter** |
| 11 | Engine-resident data; paginated extracts and governed exports; exact partition algebra; co-partition before reduction | `page_result`, `export_result`, partitioned variant | Implemented |
| 12 | Publication cuts, watermark admission, provisional latest mode, replay horizon, current-policy reauth | `cuts.py` | Simulated publications |
| 13 | Coverage-aware cache, exact/derived reuse, pinned vs latest, retained results after replay horizon, reauth every read, no partial results | `cache.py` | Implemented |
| 13 | Promotion of hot intermediates to serving artifacts | — | Boundary: shared producers reuse intermediates within lease; no promotion lifecycle |
| 14 | Trace envelope, receipts, hash chain + anchor, OTel export | `trace.py`, `_build_receipt` | Implemented (not tamper-proof storage) |
| 15 | Facts first, deterministic chart, exclusion table, confidence categories, uncertainty | `presentation.py`, Wilson operator | Implemented |
| 16 | Metric set, gate vector, null-with-reason, assessment document | `assessment.py` | Implemented |
| 17 | Case folders, lifecycle, advisory approval, scoped retrieval, shadow evaluation, decay on version change, TLP | `feedback.py`, `shadow_evaluate`, `rewrite.tlp_check` | Implemented |
| 18 | Recipe builder questions, adversarial examples, never certified | `recipe_builder.py`, `recipe-drafter` | Implemented |
| 20 | Monitoring metrics, availability states, alerts, query insights | `monitoring.py`, `insights.py` | Implemented |
| 21 | Validation matrix rows | see `tests/` (95+ tests) | Implemented for fixture scope |
| 22 | Ablation experiments | — | Boundary: harness pieces exist (replay, selector, cache); experiments not run |

## Still out of scope (and why)

* Live Snowflake / External OAuth / Cortex / Temporal / Aurora: adapters only; no credentials or services in this environment.
* Scale claims: fixture-sized data; no benchmark results are claimed.
* Survival-analysis recipe: offered in caveats only; the spec requires separate approval before certification.
