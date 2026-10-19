"""Exercise 5 — Streamlit approval UI for the HITL PR review agent.

Run with:
    uv run streamlit run app.py

Goal: wrap the LangGraph built in exercises 1–4 in a web UI that adapts to
the confidence bucket of each PR.

Routing thresholds (common/schemas.py):
    > 72%        auto_approve     UI shows a success card; reviewer does nothing
    58 – 72%     human_approval   UI shows Approve / Reject / Edit buttons
    <  58%       escalate         UI shows a question form for the reviewer,
                                  then the refined review for a final confirm
"""

from __future__ import annotations

import asyncio
import time
import uuid

import streamlit as st
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from common.db import db_conn, db_path
from common.schemas import AuditEntry, ReviewState, risk_level_for
from exercises.exercise_4_audit import (
    AGENT_ID,
    already_audited,
    audit,
    node_analyze,
    node_auto_approve,
    node_commit,
    node_escalate,
    node_fetch_pr,
    node_human_approval,
    node_route,
    node_synthesize,
    reviewer_id,
)


load_dotenv()


# ─── Graph — exercise 4 nodes + a final confirm after escalation ───────────
async def node_confirm_refined(state):
    """Show the refined (post-escalation) review and let the reviewer confirm it."""
    t0 = time.monotonic()
    a = state["analysis"]
    if not await already_audited(state, "confirm_refined", "pending"):
        await audit(state, AuditEntry(
            agent_id=AGENT_ID, action="confirm_refined",
            confidence=a.confidence, risk_level=risk_level_for(a.confidence),
            decision="pending", reason="Refined review shown to reviewer for final confirm",
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        ))

    resp = interrupt({
        "kind": "confirm_refined",
        "pr_url": state["pr_url"],
        "confidence": a.confidence,
        "confidence_reasoning": a.confidence_reasoning,
        "summary": a.summary,
        "risk_factors": a.risk_factors,
        "comments": [c.model_dump() for c in a.comments],
        "escalation_answers": state.get("escalation_answers") or {},
    })

    choice = resp.get("choice")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID, action="confirm_refined",
        confidence=a.confidence, risk_level=risk_level_for(a.confidence),
        reviewer_id=reviewer_id(), decision=choice, reason=resp.get("feedback") or None,
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    if choice == "approve":
        return {"human_choice": "approve", "human_feedback": resp.get("feedback") or None}
    # node_commit posts whenever escalation_answers is set — clear it so a
    # rejected refined review is not posted.
    return {"human_choice": "reject", "human_feedback": resp.get("feedback") or None,
            "escalation_answers": None}


def build_graph(checkpointer):
    g = StateGraph(ReviewState)
    for name, fn in [
        ("fetch_pr", node_fetch_pr), ("analyze", node_analyze), ("route", node_route),
        ("auto_approve", node_auto_approve), ("human_approval", node_human_approval),
        ("commit", node_commit), ("escalate", node_escalate), ("synthesize", node_synthesize),
        ("confirm_refined", node_confirm_refined),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "fetch_pr")
    g.add_edge("fetch_pr", "analyze")
    g.add_edge("analyze", "route")
    g.add_conditional_edges(
        "route", lambda s: s["decision"],
        {"auto_approve": "auto_approve", "human_approval": "human_approval", "escalate": "escalate"},
    )
    g.add_edge("auto_approve", END)
    g.add_edge("human_approval", "commit")
    g.add_edge("escalate", "synthesize")
    g.add_edge("synthesize", "confirm_refined")
    g.add_edge("confirm_refined", "commit")
    g.add_edge("commit", END)
    return g.compile(checkpointer=checkpointer)


# ─── Graph / DB helpers ────────────────────────────────────────────────────
async def run_graph(pr_url: str, thread_id: str, resume_value=None):
    """Invoke the graph once. Returns the final result or {'__interrupt__': ...}."""
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        app = build_graph(cp)
        cfg = {"configurable": {"thread_id": thread_id}}
        if resume_value is None:
            return await app.ainvoke({"pr_url": pr_url, "thread_id": thread_id}, cfg)
        return await app.ainvoke(Command(resume=resume_value), cfg)


async def load_thread(thread_id: str) -> tuple[dict, dict | None]:
    """Recover (state values, pending interrupt payload) from the checkpointer."""
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        snap = await build_graph(cp).aget_state({"configurable": {"thread_id": thread_id}})
    payload = snap.interrupts[0].value if snap.interrupts else None
    return snap.values, payload


async def recent_threads(limit: int = 15) -> list[dict]:
    """Like audit.replay.list_threads, but ranks risk high > med > low correctly."""
    async with db_conn() as conn:
        async with conn.execute(
            """
            SELECT thread_id, pr_url,
                   MAX(timestamp) AS last_event,
                   MAX(CASE risk_level WHEN 'high' THEN 3 WHEN 'med' THEN 2 ELSE 1 END) AS risk_rank,
                   COUNT(*) AS events
              FROM audit_events
             GROUP BY thread_id, pr_url
             ORDER BY MAX(timestamp) DESC
             LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


def open_thread(thread_id: str) -> None:
    """Point the UI at an existing thread and rebuild its view from the checkpoint."""
    values, payload = asyncio.run(load_thread(thread_id))
    st.session_state.thread_id = thread_id
    st.session_state.pr_url = values.get("pr_url", "")
    st.session_state.interrupt_payload = payload
    st.session_state.final = values if payload is None and values.get("final_action") else None
    st.query_params["thread"] = thread_id


def apply_result(result: dict) -> None:
    if "__interrupt__" in result:
        st.session_state.interrupt_payload = result["__interrupt__"][0].value
        st.session_state.final = None
    else:
        st.session_state.interrupt_payload = None
        st.session_state.final = result


# ─── Session state ─────────────────────────────────────────────────────────
st.set_page_config(page_title="HITL PR Review", layout="wide")

if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
if "pr_url" not in st.session_state:
    st.session_state.pr_url = ""
if "interrupt_payload" not in st.session_state:
    st.session_state.interrupt_payload = None
if "final" not in st.session_state:
    st.session_state.final = None

# A browser reload wipes session_state; the thread id in the URL brings it back.
if st.session_state.thread_id is None and st.query_params.get("thread"):
    open_thread(st.query_params["thread"])


# ─── Page setup ────────────────────────────────────────────────────────────
st.title("HITL PR Review Agent")


# ─── Sidebar — recent sessions ─────────────────────────────────────────────
RISK_BADGE = {3: "🔴 high", 2: "🟡 med", 1: "🟢 low"}

with st.sidebar:
    st.header("Recent sessions")
    threads = asyncio.run(recent_threads())
    if not threads:
        st.caption("No sessions yet — run a review.")
    for t in threads:
        pr_ref = t["pr_url"].split("github.com/")[-1].replace("/pull/", " #")
        current = t["thread_id"] == st.session_state.thread_id
        label = (f"{'▶ ' if current else ''}{pr_ref} · {RISK_BADGE.get(t['risk_rank'], '?')}"
                 f" · {t['thread_id'][:8]}")
        if st.button(label, key=f"thread-{t['thread_id']}", use_container_width=True):
            open_thread(t["thread_id"])
            st.rerun()
        st.caption(f"{t['events']} events · {t['last_event'][:19].replace('T', ' ')} UTC")


# ─── Top form — start a new review ─────────────────────────────────────────
with st.form("start"):
    pr_url = st.text_input(
        "PR URL", value=st.session_state.pr_url,
        placeholder="https://github.com/VinUni-AI20k/PR-Demo/pull/1",
    )
    submitted = st.form_submit_button("Run review")


# ─── Renderers per interrupt kind ──────────────────────────────────────────
def render_comments(comments: list[dict]) -> None:
    for c in comments:
        st.markdown(f"- **[{c['severity']}]** `{c['file']}:{c.get('line') or '?'}` — {c['body']}")


def render_approval_card(payload: dict) -> dict | None:
    """58–72% bucket: show the LLM review + 3 buttons. Return resume dict or None."""
    conf = payload["confidence"]
    st.subheader(f"Approval requested — confidence {conf:.0%}")
    st.caption(payload["confidence_reasoning"])
    st.markdown(payload["summary"])

    render_comments(payload.get("comments", []))

    with st.expander("Diff"):
        st.code(payload.get("diff_preview", ""), language="diff")

    feedback = st.text_input("Feedback (optional)", key="approval_feedback")
    col1, col2, col3 = st.columns(3)
    if col1.button("Approve", type="primary", use_container_width=True):
        return {"choice": "approve", "feedback": feedback}
    if col2.button("Reject", use_container_width=True):
        return {"choice": "reject", "feedback": feedback}
    if col3.button("Edit", use_container_width=True):
        if not feedback.strip():
            st.warning("Write what should change in the feedback box before choosing Edit.")
            return None
        return {"choice": "edit", "feedback": feedback}
    return None


def render_escalation_card(payload: dict) -> dict | None:
    """< 58% bucket: show risk factors + question form. Return {question: answer} or None."""
    conf = payload["confidence"]
    st.subheader(f"Strong escalation — confidence {conf:.0%}")
    st.caption(payload["confidence_reasoning"])
    if payload.get("risk_factors"):
        st.error("**Risks**\n\n" + "\n".join(f"- {r}" for r in payload["risk_factors"]))
    st.markdown(payload["summary"])

    with st.form("escalation"):
        st.markdown("**The agent needs answers to these questions before it can review:**")
        answers: dict[str, str] = {}
        for i, q in enumerate(payload["questions"]):
            answers[q] = st.text_area(q, key=f"escalation_q{i}", height=80)
        if st.form_submit_button("Submit answers", type="primary"):
            if any(not a.strip() for a in answers.values()):
                st.warning("Please answer every question.")
                return None
            return {q: a.strip() for q, a in answers.items()}
    return None


def render_confirm_card(payload: dict) -> dict | None:
    """After escalation: show the refined review for a final confirm."""
    conf = payload["confidence"]
    st.subheader(f"Refined review — confidence {conf:.0%}")
    st.caption(payload["confidence_reasoning"])
    st.markdown(payload["summary"])
    render_comments(payload.get("comments", []))
    with st.expander("Your answers"):
        for q, a in payload.get("escalation_answers", {}).items():
            st.markdown(f"**{q}**\n\n> {a}")

    feedback = st.text_input("Note for the posted comment (optional)", key="confirm_feedback")
    col1, col2 = st.columns(2)
    if col1.button("Post refined review", type="primary", use_container_width=True):
        return {"choice": "approve", "feedback": feedback}
    if col2.button("Discard", use_container_width=True):
        return {"choice": "reject", "feedback": feedback}
    return None


RENDERERS = {
    "approval_request": render_approval_card,
    "escalation": render_escalation_card,
    "confirm_refined": render_confirm_card,
}


# ─── Main flow ─────────────────────────────────────────────────────────────
if submitted and pr_url:
    st.session_state.pr_url = pr_url
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.interrupt_payload = None
    st.session_state.final = None
    st.query_params["thread"] = st.session_state.thread_id

    with st.spinner("Fetching PR + asking the LLM..."):
        try:
            apply_result(asyncio.run(run_graph(pr_url, st.session_state.thread_id)))
        except Exception as e:
            st.error(f"Review failed: {e}")

# Render the current interrupt card, if any
payload = st.session_state.interrupt_payload
if payload is not None:
    answer = RENDERERS[payload["kind"]](payload)
    if answer is not None:
        with st.spinner("Resuming..."):
            apply_result(asyncio.run(run_graph(
                st.session_state.pr_url, st.session_state.thread_id, resume_value=answer,
            )))
        st.rerun()

# Render final state, if reached
if st.session_state.final is not None:
    final = st.session_state.final
    action = final.get("final_action", "?")
    a = final.get("analysis")
    pr_link = st.session_state.pr_url
    if action.startswith("auto") and "failed" not in action:
        st.success(f"✓ Auto-approved — confidence {a.confidence:.0%}. "
                   "The agent posted the review itself; nothing for you to do.")
        st.markdown(a.summary)
        st.link_button("View comment on GitHub", pr_link)
    elif action.startswith("committed"):
        st.success(f"✓ {action} — comment posted to {pr_link}")
        st.link_button("View comment on GitHub", pr_link)
    elif action == "rejected":
        choice = final.get("human_choice")
        st.warning(f"No comment posted (reviewer chose **{choice}**)"
                   + (f": {final['human_feedback']}" if final.get("human_feedback") else ""))
    elif "failed" in action:
        st.error(f"{action} — the review could not be posted to GitHub. Check that GITHUB_TOKEN "
                 "has the `public_repo` scope.")
        if a is not None:
            st.markdown(a.summary)
    else:
        st.info(f"final_action = {action}")

if st.session_state.thread_id:
    st.caption(f"thread_id = {st.session_state.thread_id}  ·  replay: "
               f"`uv run python -m audit.replay --thread {st.session_state.thread_id}`")
