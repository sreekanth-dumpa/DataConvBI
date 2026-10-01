"""Streamlit UI for the Information Path Routing reference POC.

Streamlit renders status and small validated aggregates; it sends requests to
the always-running in-process execution service and never becomes the
high-volume processing engine.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from ipr.config import Settings
from ipr.intent import Requester
from ipr.fixture import SUBJECTS
from ipr.recipe_builder import QUESTIONS
from ipr.agents.providers import ClaudeProvider, OfflineProvider, ReplayProvider
from ipr.agents.stability import evaluate_stability
from ipr.service import Gap, IPRService

DEMO_Q = ("What was the 30-day quote conversion for NC personal auto quotes issued in August 2026 "
          "by product version and channel, as of 2026-09-15?")

st.set_page_config(page_title="Information Path Routing", layout="wide")


@st.cache_resource
def get_service() -> IPRService:
    return IPRService(Settings(home=Path(os.environ.get("IPR_HOME", ".ipr_data")).resolve()))


svc = get_service()
ss = st.session_state
ss.setdefault("request_id", None)
ss.setdefault("execution_id", None)
ss.setdefault("reuse", None)
ss.setdefault("message", None)

st.title("Agentic Information Path Routing")
st.caption("Evidence-bound information compiler · reference POC on a synthetic fixture · "
           f"business clock {svc.clock.now_z()}")

with st.sidebar:
    subject = st.selectbox("Requester (delegated identity)", list(SUBJECTS), index=0)
    st.caption(f"Entitled states: {', '.join(SUBJECTS[subject][0])}")
    st.divider()
    st.caption("Agent skills provider")
    provider_names = ["offline", "claude", "replay"]
    current = svc.agents.provider.name
    choice = st.radio("LLM provider", provider_names, index=provider_names.index(current) if current in provider_names else 0,
                      horizontal=True, help="offline = deterministic skill engine; claude needs ANTHROPIC_API_KEY; replay = frozen proposals")
    if choice != current:
        try:
            svc.agents.provider = {"offline": OfflineProvider, "replay": lambda: ReplayProvider(svc.store),
                                   "claude": lambda: ClaudeProvider()}[choice]()
        except Exception as exc:  # noqa: BLE001
            st.error(f"provider unavailable: {exc}")
    st.divider()
    prefs = svc.preferences(subject)
    with st.expander("Admission preferences"):
        auto = st.checkbox("Auto-admit within envelope", prefs.get("auto_admit", True))
        mc = st.number_input("Max simulated credits", value=float(prefs.get("max_credits") or 2.0), step=0.1)
        ml = st.number_input("Max p90 latency (s)", value=float(prefs.get("max_latency_s") or 900.0), step=10.0)
        if st.button("Save preferences"):
            svc.set_preferences(subject, {"auto_admit": auto, "max_credits": mc, "max_latency_s": ml})
    if st.button("Reset conversation"):
        for k in ("request_id", "execution_id", "reuse", "message"):
            ss[k] = None

tabs = st.tabs(["Ask", "Execution", "Plan & obligations", "Agent skills", "Cache", "Assess & feedback", "Recipe builder",
                "Monitoring", "Trace"])


def show_answer(ans: dict) -> None:
    facts = ans["facts"]
    t = facts["totals"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Conversion (mature)", "n/a" if t["conversion_rate"] is None else f"{t['conversion_rate']:.1%}")
    c2.metric("Mature opportunities", f"{t['eligible_mature']:,}")
    c3.metric("Immature excluded", f"{t['immature']:,}")
    c4.metric("Origin", ans["origin"])
    spec = ans["chart"]
    df = pd.DataFrame(spec["data"]["values"])
    if not df.empty:
        dims = facts["dimensions"]
        x = dims[0] if dims else "label"
        chart = alt.Chart(df).mark_bar().encode(
            x=alt.X(f"{x}:N", title=x.replace("_", " ")),
            y=alt.Y("conversion_rate:Q", axis=alt.Axis(format=".0%"), title="Conversion rate (mature)"),
            tooltip=list(df.columns))
        if len(dims) > 1:
            chart = chart.encode(xOffset=f"{dims[1]}:N", color=f"{dims[1]}:N")
        st.altair_chart(chart, width="stretch")
    st.dataframe(pd.DataFrame(facts["by_dimension"]), width="stretch")
    nar = ans.get("agent_narrative") or {}
    if nar.get("headline"):
        st.markdown(f"**{nar['headline']}**")
        st.write(nar["narrative"])
        ag = nar.get("agent", {})
        st.caption(f"answer-narrator via {ag.get('provider')} · {len(nar.get('claims', []))} numeric claims verified against validated facts"
                   + (" · fell back to offline engine" if ag.get("fallback_used") else "")
                   + f" · chart recommendation: {nar.get('chart_recommendation', {}).get('type')}")
    with st.expander("Deterministic narrative and caveats"):
        st.text(ans["narrative"])
    st.table(pd.DataFrame(ans["confidence"]))


# --------------------------------------------------------------------- Ask
with tabs[0]:
    q = st.text_area("Question", DEMO_Q, height=80)
    if st.button("Interpret and ground", type="primary"):
        st_ = svc.interpret(q, Requester(subject=subject), parent_request_id=ss.request_id)
        ss.request_id = st_.record.request_id
        ss.execution_id = None
        ss.reuse = None
        svc.ground(ss.request_id)
    rid = ss.request_id
    if rid:
        rs = svc.requests[rid]
        res = rs.resolution
        st.subheader(f"Request {rid} — {res.status}")
        if res.route:
            st.success(f"Recipe: {res.route['title']} ({res.route['recipe_id']}@{res.route['version']}, {res.route['status']})")
        for g in res.gaps:
            st.error(g)
        if res.unsupported:
            st.warning("Unsupported meaning: " + ", ".join(res.unsupported) + " — use the Recipe builder tab.")
        if res.advisory_feedback:
            st.info("Approved advisory feedback applies: " + json.dumps(res.advisory_feedback)[:400])
        mp = rs.meaning_presentation or {}
        if mp.get("confirmation_summary"):
            st.info(mp["confirmation_summary"])
            st.caption(f"clarification-writer via {mp['agent']['provider']} (presentation only; the semantic hash uses the typed intent)")
        if res.semantic_paths:
            st.caption("Semantic graph: " + "; ".join(f"{p['dimension']}: {p['status']}" for p in res.semantic_paths))
        business_q = {q["id"]: q for q in mp.get("questions", [])}
        open_q = [qq for qq in res.questions]
        if open_q:
            with st.form("clarify"):
                answers = {}
                for qq in open_q:
                    default = json.dumps(qq.recommended) if isinstance(qq.recommended, dict) else str(qq.recommended or "")
                    label = business_q.get(qq.id, {}).get("business_question", qq.question)
                    answers[qq.field] = st.text_input(label, default, key=f"q_{qq.id}")
                if st.form_submit_button("Answer clarifications"):
                    parsed = {}
                    for k, v in answers.items():
                        try:
                            parsed[k] = json.loads(v) if v.strip().startswith("{") else (int(v) if v.isdigit() else v)
                        except json.JSONDecodeError:
                            parsed[k] = v
                    svc.clarify(rid, parsed)
                    st.rerun()
        with st.expander("Recipe-grounded intent (confirm this meaning)", expanded=res.status == "resolved"):
            st.json(rs.record.intent.model_dump(mode="json"))
            st.caption(f"Semantic hash: {svc.request_summary(rid)['semantic_hash']}")
            st.write("Assumptions:", rs.record.assumptions or "none")
        if res.status == "resolved" and rs.record.confirmation.status != "confirmed":
            if st.button("Confirm meaning"):
                svc.confirm(rid, subject)
                st.rerun()
        if rs.record.confirmation.status == "confirmed":
            st.caption(f"Confirmed by {rs.record.confirmation.actor}: {rs.record.confirmation.approved_semantic_hash}")
            if rs.prepared is None and st.button("Resolve sources and check reuse"):
                try:
                    p = svc.prepare(rid)
                    if p["cache"].outcome != "miss":
                        ss.reuse = svc.reuse_answer(rid)
                except Gap as g:
                    st.error(f"{g.kind}: {g}")
                    st.json(g.detail)
                st.rerun()
            if rs.prepared is not None:
                p = rs.prepared
                st.write(f"Source cut ({p['cut'].cut_mode}): " + ", ".join(e.publication_id for e in p["cut"].entries)
                         + f" · replayable until {p['cut'].replayable_until}")
                st.write(f"Cache: **{p['cache'].outcome}** · saved plan: {p['saved_plan']}")
                if p["cache"].rejected:
                    st.caption("Rejected cache candidates: " + json.dumps(p["cache"].rejected)[:600])
                if ss.reuse:
                    show_answer(ss.reuse)
                elif rs.plan is None:
                    if st.button("Plan"):
                        try:
                            svc.plan(rid)
                        except Gap as g:
                            st.error(f"{g.kind}: {g}")
                            st.json(g.detail)
                        st.rerun()
                else:
                    est_rows = [{"variant": c["variant"], "admissible": c["admissible"], **c["estimate"].to_dict()} for c in rs.plan["candidates"]]
                    st.dataframe(pd.DataFrame(est_rows)[["variant", "admissible", "latency_p50_s", "latency_p90_s", "credits_low",
                                                         "credits_high", "stage_writes", "feasible", "objective"]], width="stretch")
                    st.caption(f"Selected: {rs.plan['selected']['variant']} · Pareto frontier: {rs.plan['frontier']} · "
                               "credits are simulated credit-equivalents (admission proxy, not invoice ceilings)")
                    variant = st.selectbox("Variant", [c["variant"] for c in rs.plan["candidates"] if c["admissible"]],
                                           index=[c["variant"] for c in rs.plan["candidates"] if c["admissible"]].index(rs.plan["selected"]["variant"]))
                    st.caption(f"Selection: {rs.plan.get('selection_reason')} · learned selector: {rs.plan['learned_selection']['mode']}")
                    adm = svc.admission_decision(rid, variant)
                    if adm["reasons"]:
                        st.warning("Outside your admission envelope: " + "; ".join(adm["reasons"]))
                    defer = st.checkbox("Defer start by 1 hour (queue)")
                    if ss.execution_id is None and st.button("Execute", type="primary"):
                        from datetime import timedelta
                        later = (svc.clock.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ") if defer else None
                        ss.execution_id = svc.execute(rid, variant=variant, defer_until=later,
                                                      admission_basis="explicit_choice")
                        st.rerun()
    if ss.execution_id:
        stat = svc.runtime.status(ss.execution_id)
        state = stat["execution"]["state"]
        st.write(f"Execution `{ss.execution_id}` — **{state}**")
        if state in ("admitted", "running", "queued"):
            if st.button("Cancel execution"):
                svc.cancel(ss.execution_id)
            time.sleep(0.3)
            st.rerun()
        elif state == "succeeded":
            show_answer(svc.result(ss.execution_id))
            c1, c2 = st.columns(2)
            if c1.button("Show result page 1 (re-authorized read)"):
                st.dataframe(pd.DataFrame(svc.page_result(ss.execution_id, {"subject": subject}, 0, 50)["rows"]), width="stretch")
            if c2.button("Export governed CSV"):
                try:
                    exp = svc.export_result(ss.execution_id, {"subject": subject})
                    st.success(f"Exported {exp['manifest']['rows']} rows → {exp['path']}")
                except Gap as g:
                    st.error(str(g))
        else:
            st.error(stat["execution"]["error"])

# --------------------------------------------------------------------- Execution
with tabs[1]:
    if ss.execution_id:
        stat = svc.runtime.status(ss.execution_id)
        st.json(stat["execution"], expanded=False)
        st.dataframe(pd.DataFrame([{k: v for k, v in n.items() if k != "observations"} |
                                   {"rows": (n["observations"] or {}).get("rows"), "elapsed_ms": (n["observations"] or {}).get("elapsed_ms")}
                                   for n in stat["nodes"]]), width="stretch")
        st.dataframe(pd.DataFrame([{k: a[k] for k in ("attempt_id", "node_id", "state", "query_id", "query_tag", "output_relation")}
                                   for a in stat["attempts"]]), width="stretch")
        r = svc.store.one("SELECT receipt_json FROM receipts WHERE execution_id=?", (ss.execution_id,))
        if r:
            with st.expander("Execution receipt"):
                st.json(json.loads(r["receipt_json"]))
    else:
        st.info("No execution yet.")

# --------------------------------------------------------------------- Plan
with tabs[2]:
    rid = ss.request_id
    if rid and svc.requests[rid].plan:
        pl = svc.requests[rid].plan
        st.write(f"Plan `{pl['plan_id']}` v{pl['version']} · logical `{pl['logical']['logical_fingerprint'][:23]}`")
        with st.expander("Logical IR"):
            st.json(pl["logical"])
        sel = pl["selected"]
        with st.expander("Obligation manifest (Pass B, frozen)"):
            ob = sel["obligations"].to_dict()
            st.dataframe(pd.DataFrame([{"id": k, "status": v["status"], "severity": v["severity"],
                                        "origins": ", ".join(o["kind"] + ":" + o["ref"] for o in v["origins"])[:120],
                                        "validation": v["validation"], "reason": v["reason"]} for k, v in ob["obligations"].items()]),
                         width="stretch")
        with st.expander("Certificate"):
            st.json(sel["certificate"])
        with st.expander("Physical SQL (selected)"):
            for n in sel["physical"]["nodes"]:
                if n["kind"] == "sql_ctas":
                    st.markdown(f"**{n['id']}** ({n['operator']}) deps={n['depends_on']}")
                    st.code(n["sql"], language="sql")
        with st.expander("Probe log"):
            st.json(pl["stats"].get("probe_log"))
        with st.expander("Agent proposals (plan-proposer, obligation-proposer, probe-advisor)"):
            st.json(pl["agents"])
        with st.expander("Child graphs and EXPLAIN planning information"):
            st.json(sel["physical"].get("child_graphs"))
            st.json(sel.get("explain", {}))
        with st.expander("Learned selection"):
            st.json(pl["learned_selection"])
    elif rid and svc.requests[rid].resolution:
        st.json(svc.requests[rid].resolution.to_dict()["obligations_a"] or {})
    else:
        st.info("No plan yet.")

# --------------------------------------------------------------------- Agent skills
with tabs[3]:
    st.caption(f"Provider: **{svc.agents.provider.name}** · agents propose; the compiler, guards, validators and policies decide.")
    for name, sk in svc.agents.skills.items():
        with st.expander(f"{name} @ {sk.version} — {sk.phase} · {sk.authority}"):
            st.write(sk.description)
            st.markdown("**May:** " + "; ".join(sk.may))
            st.markdown("**May not:** " + "; ".join(sk.may_not))
            st.code(sk.system_prompt, language="markdown")
            st.json(sk.schema, expanded=False)
    rows = svc.agents.invocations(request_id=ss.request_id) if ss.request_id else svc.agents.invocations(limit=50)
    if rows:
        st.subheader("Invocation receipts")
        st.dataframe(pd.DataFrame([{k: r[k] for k in ("skill", "provider", "model", "fallback_used", "fallback_reason", "latency_ms",
                                                      "prompt_hash", "response_hash")} for r in rows]), width="stretch")
    if ss.request_id and st.button("Check proposal stability (intent-interpreter x5, concurrency 3)"):
        st.json(evaluate_stability(svc.agents, "intent-interpreter",
                                   {"text": svc.requests[ss.request_id].record.raw_text,
                                    "dimension_vocabulary": sorted(svc.semantic_dimension_vocabulary()),
                                    "today_local": svc.clock.now_z()[:10]}, repeats=5, concurrency=3))

# --------------------------------------------------------------------- Cache
with tabs[4]:
    st.dataframe(pd.DataFrame(svc.cache.entries()), width="stretch")
    if st.button("Evict expired entries"):
        st.write(svc.cache.evict_expired(), "evicted")

# --------------------------------------------------------------------- Assess
with tabs[5]:
    if ss.execution_id and svc.runtime.status(ss.execution_id)["execution"]["state"] == "succeeded":
        fb = st.text_area("Your feedback (stored as a data item)")
        cmp = st.checkbox("Replay all admitted variants on the same cut (plan equivalence)")
        if st.button("Assess feedback"):
            a = svc.assess(ss.execution_id, fb or None, compare_variants=cmp)
            ss.last_case = a["case"]["case_id"]
            st.markdown(a["document"])
    for c in svc.feedback.all():
        cols = st.columns([3, 2, 2, 2])
        cols[0].write(f"`{c['case_id']}` {c['promotion']['change_class']} — {c['recommendations'][0]['recommendation'][:80]}")
        cols[1].write(c["promotion"]["state"])
        if c["promotion"]["state"] == "proposed" and cols[2].button("Approve as advisory", key=f"ap_{c['case_id']}"):
            svc.feedback.transition(c["case_id"], "approved", subject, "approved as advisory context")
            st.rerun()
        if c["promotion"]["state"] == "proposed" and cols[3].button("Reject", key=f"rj_{c['case_id']}"):
            svc.feedback.transition(c["case_id"], "rejected", subject)
            st.rerun()

# --------------------------------------------------------------------- Recipe builder
with tabs[6]:
    st.caption("Drafts are never certified by being generated. No user SQL is accepted.")
    logic = st.text_area("Business logic in your words",
                         "Share of quotes that bind within 30 local calendar days, counted once per submission, excluding binds cancelled in the window",
                         key="rb_logic")
    if st.button("Draft answers with recipe-drafter skill"):
        ss.rb_proposal = svc.draft_recipe_answers(logic)
    proposal = ss.get("rb_proposal")
    proposed = {a["question_id"]: a["option"] for a in (proposal or {}).get("answers", [])}
    if proposal:
        st.caption(f"Agent proposal via {proposal['agent']['provider']}; unresolved: {proposal['unresolved']} — review every answer.")
    with st.form("builder"):
        title = st.text_input("Recipe title", (proposal or {}).get("title") or "Net quote conversion")
        answers = {}
        for qq in QUESTIONS:
            opts = list(qq["options"])
            idx = opts.index(proposed[qq["id"]]) if qq["id"] in proposed else 0
            answers[qq["id"]] = st.radio(qq["question"] + (" (agent proposal)" if qq["id"] in proposed else " (unresolved)"), opts, index=idx,
                                         format_func=lambda k, q=qq: q["options"][k], key=f"rb_{qq['id']}")
        if st.form_submit_button("Generate draft"):
            d = svc.build_recipe_draft(logic, answers, title=title, owner=subject, dimensions=["product_version"], agent_proposal=proposal)
            st.json(d["installed_algebra_match"])
            st.caption(f"Saved {d['draft_ref']}")
    st.dataframe(pd.DataFrame([{k: d[k] for k in ("id", "version", "status", "title")} for d in svc.builder.drafts()]),
                 width="stretch")

# --------------------------------------------------------------------- Monitoring
with tabs[7]:
    snap = svc.monitoring()
    for a in snap["alerts"]:
        st.warning(a)
    st.json(snap["metrics"])

# --------------------------------------------------------------------- Trace
with tabs[8]:
    v = svc.trace.verify()
    (st.success if v["valid"] else st.error)(json.dumps(v))
    if ss.request_id:
        st.dataframe(pd.DataFrame([{k: e.get(k) for k in ("seq", "stage", "status", "execution_id", "hash")}
                                   for e in svc.trace.events(request_id=ss.request_id)]), width="stretch")
