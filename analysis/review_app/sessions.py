"""Build reviewable sessions from Cartwheel traces.

Cartwheel creates one Langfuse trace per user turn, so a multi-turn
conversation arrives as several traces. Review happens per conversation:
this module groups traces by ``cartwheel.session_id`` (not by scenario, since
one scenario can run in more than one session), orders the turns in time, and
turns each trace into a compact timeline a human can read top to bottom.

Every GENERATION span repeats the system prompt and the whole history, so the
timeline keeps only what is new at each step:

    user message -> narration -> tool call + result -> ... -> final reply

Narration is the text the model emits next to a tool call ("I'll search the
catalog..."). Only the final output reaches the user (``server/app.py``), so
narration is kept but marked as not shown to the user.

Langfuse is the canonical source. The built sessions are cached under
``analysis/review_app/.cache/`` (git-ignored). When Langfuse is unavailable the
Module 1 export ``traces/support_traces.json`` is the fallback, and the cache
records which source it came from.

Run it:

    uv run python -m analysis.review_app.sessions --refresh     # rebuild cache
    uv run python -m analysis.review_app.sessions --show 4b23   # print one session
"""

from __future__ import annotations

import argparse
import ast
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis.helpers.normalization import normalize_trace

ROOT = Path(__file__).resolve().parents[2]
CACHE_PATH = Path(__file__).resolve().parent / ".cache" / "sessions.json"
EXPORT_PATH = ROOT / "traces" / "support_traces.json"
SCENARIO_FILES = sorted((ROOT / "scenarios").glob("*_scenarios.jsonl"))

# Tools with side effects (SPEC.md section 4, risk tier "write").
WRITE_TOOLS = {"issue_refund", "cancel_order", "escalate_to_human"}

# Result keys holding the list a read tool returns; an empty list is a badge.
LIST_KEYS = ("products", "orders", "results")


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def _parse(value: Any) -> Any:
    """Return a tool payload as a Python object.

    Langfuse returns tool inputs and outputs either as objects, JSON strings,
    or Python-repr strings (``{'ok': True}``). Unparseable text is returned
    unchanged so nothing is silently dropped.
    """
    if not isinstance(value, str):
        return value
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(value)
        except (ValueError, SyntaxError):
            continue
    return value


def _message_texts(messages: Any, role: str | None = None) -> list[str]:
    """Collect the text parts of ``[{"role", "parts": [...]}]`` messages."""
    messages = _parse(messages)
    if isinstance(messages, str):
        return [messages]
    texts: list[str] = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        if role is not None and message.get("role") != role:
            continue
        for part in message.get("parts") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                content = str(part.get("content") or "").strip()
                if content:
                    texts.append(content)
    return texts


def _has_tool_call(output: Any) -> bool:
    output = _parse(output)
    for message in output if isinstance(output, list) else []:
        for part in (message.get("parts") or []) if isinstance(message, dict) else []:
            if isinstance(part, dict) and part.get("type") == "tool_call":
                return True
    return False


def _system_prompt(observations: list[dict[str, Any]]) -> str | None:
    for obs in observations:
        if obs.get("type") != "GENERATION":
            continue
        payload = _parse(obs.get("input"))
        messages = payload.get("messages") if isinstance(payload, dict) else None
        texts = _message_texts(messages, role="system")
        if texts:
            return "\n".join(texts)
    return None


def _tool_badges(name: str, result: Any) -> list[str]:
    badges: list[str] = []
    if name in WRITE_TOOLS:
        badges.append("write")
    if isinstance(result, dict):
        if result.get("ok") is False:
            badges.append(f"error:{result.get('error') or 'unknown'}")
        elif any(
            isinstance(result.get(key), list) and not result[key] for key in LIST_KEYS
        ):
            badges.append("empty_result")
    return badges


# ---------------------------------------------------------------------------
# one trace -> one turn
# ---------------------------------------------------------------------------


def build_turn(trace: dict[str, Any]) -> dict[str, Any]:
    """Turn one normalized trace (one user turn) into a deduplicated timeline.

    Tool steps come from TOOL spans, which carry both arguments and result.
    The ``tool_call`` parts inside GENERATION outputs describe the same calls,
    so they are not repeated. The last GENERATION without a tool call
    produced the final reply, which is taken from the trace output.
    """
    observations = sorted(
        trace.get("observations") or [], key=lambda o: o.get("start_time") or ""
    )
    generations = [o for o in observations if o.get("type") == "GENERATION"]
    final = next(
        (g for g in reversed(generations) if not _has_tool_call(g.get("output"))),
        None,
    )

    steps: list[dict[str, Any]] = []
    for obs in observations:
        if obs.get("type") == "GENERATION" and obs is not final:
            for text in _message_texts(obs.get("output")):
                steps.append({"kind": "narration", "text": text})
        elif obs.get("type") == "TOOL":
            name = str(obs.get("name") or "tool")
            result = _parse(obs.get("output"))
            steps.append(
                {
                    "kind": "tool",
                    "name": name,
                    "arguments": _parse(obs.get("input")),
                    "result": result,
                    "badges": _tool_badges(name, result),
                }
            )

    reply = "\n".join(_message_texts(trace.get("output")))
    if not reply and final is not None:
        reply = "\n".join(_message_texts(final.get("output")))
    user = _message_texts(trace.get("input"), role="user")
    return {
        "trace_id": trace["trace_id"],
        "timestamp": trace.get("timestamp"),
        "user": user[-1] if user else "",
        "steps": steps,
        "reply": reply,
    }


# ---------------------------------------------------------------------------
# traces -> sessions
# ---------------------------------------------------------------------------


def load_scenarios() -> dict[str, dict[str, Any]]:
    """Index the Module 1 scenario plans by scenario id."""
    scenarios: dict[str, dict[str, Any]] = {}
    for path in SCENARIO_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                scenarios[str(record.get("id"))] = record
    return scenarios


def build_sessions(
    traces: list[dict[str, Any]],
    scenarios: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Group normalized traces by session id and build one record per session.

    Traces without a session id (manual runs outside Module 1) are skipped.
    Sessions are returned oldest first.
    """
    scenarios = scenarios or {}
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trace in traces:
        session_id = (trace.get("meta") or {}).get("session_id")
        if session_id:
            by_session[str(session_id)].append(trace)

    sessions: list[dict[str, Any]] = []
    for session_id, group in by_session.items():
        group.sort(key=lambda t: t.get("timestamp") or "")
        first = group[0]
        meta = first.get("meta") or {}
        turns = [build_turn(trace) for trace in group]
        tools = sorted(
            {s["name"] for turn in turns for s in turn["steps"] if s["kind"] == "tool"}
        )
        badges = sorted(
            {b for turn in turns for s in turn["steps"] for b in s.get("badges", [])}
        )
        if len(turns) > 1:
            badges.append("multi_turn")
        if any(not turn["reply"] for turn in turns):
            # The run ended without a final answer (e.g. the turn limit).
            badges.append("no_reply")
        if not tools:
            badges.append("no_tools")
        scenario_id = meta.get("scenario_id")
        sessions.append(
            {
                "session_id": session_id,
                "scenario_id": scenario_id,
                "role": meta.get("role"),
                "user_id": (first.get("metadata") or {}).get("cartwheel.user_id"),
                "store": meta.get("store"),
                "prompt_version": meta.get("prompt_version"),
                "started_at": first.get("timestamp"),
                "trace_ids": [t["trace_id"] for t in group],
                "turn_count": len(turns),
                "tools": tools,
                "badges": badges,
                "system_prompt": _system_prompt(
                    sorted(
                        first.get("observations") or [],
                        key=lambda o: o.get("start_time") or "",
                    )
                ),
                "scenario": scenarios.get(str(scenario_id)) if scenario_id else None,
                "turns": turns,
            }
        )
    sessions.sort(key=lambda s: s["started_at"] or "")
    return sessions


# ---------------------------------------------------------------------------
# loading and caching
# ---------------------------------------------------------------------------


def _langfuse_traces() -> tuple[list[dict[str, Any]], str | None]:
    """Pull Module 1 traces and the project id (for trace links) from Langfuse."""
    from analysis.helpers import langfuse_io

    client = langfuse_io._client()
    traces = langfuse_io.fetch_traces(limit=5000, client=client)
    project_id = None
    try:
        projects = client.api.projects.get()
        project_id = projects.data[0].id if projects.data else None
    except Exception:  # links are a convenience, not required
        pass
    return traces, project_id


def _export_traces() -> list[dict[str, Any]]:
    data = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))
    records = data.get("traces", []) if isinstance(data, dict) else data
    return [normalize_trace(record) for record in records]


def refresh(source: str = "langfuse") -> dict[str, Any]:
    """Rebuild the session cache from ``langfuse`` or the Module 1 ``export``.

    With ``source="langfuse"``, a connection failure falls back to the export
    and records the reason, which belongs in the interface comparison.
    """
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    project_id = None
    fallback_reason = None
    if source == "langfuse":
        try:
            traces, project_id = _langfuse_traces()
        except Exception as exc:
            fallback_reason = f"Langfuse unavailable: {exc}"
            source = "export"
    if source == "export":
        traces = _export_traces()

    sessions = build_sessions(traces, load_scenarios())
    host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    payload = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "fallback_reason": fallback_reason,
        "langfuse_trace_url": (
            f"{host}/project/{project_id}/traces/" if host and project_id else None
        ),
        "trace_count": sum(s["turn_count"] for s in sessions),
        "session_count": len(sessions),
        "sessions": sessions,
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str))
    tmp.replace(CACHE_PATH)
    return payload


def load_cached() -> dict[str, Any]:
    """Return the cached sessions, building them on first use."""
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return refresh()


# ---------------------------------------------------------------------------
# text rendering (for checking a session without the UI)
# ---------------------------------------------------------------------------


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f" ...[{len(text)} chars]"


def render_text(session: dict[str, Any]) -> str:
    lines = [
        f"SESSION {session['session_id']}  role={session['role']}  "
        f"scenario={session['scenario_id']}  turns={session['turn_count']}",
        f"tools={session['tools']}  badges={session['badges']}",
        f"system prompt: {len(session['system_prompt'] or '')} chars (shown once)",
    ]
    for index, turn in enumerate(session["turns"], start=1):
        end = "  [END OF SESSION]" if index == session["turn_count"] else ""
        lines.append(f"\n--- Turn {index} of {session['turn_count']}  trace {turn['trace_id']}{end}")
        lines.append(f"USER: {turn['user']}")
        for step in turn["steps"]:
            if step["kind"] == "narration":
                lines.append(f"  (narration, not shown to user) {step['text']}")
            else:
                badges = f"  <{', '.join(step['badges'])}>" if step["badges"] else ""
                lines.append(f"  TOOL {step['name']}{badges}")
                lines.append(f"    args:   {_short(step['arguments'])}")
                lines.append(f"    result: {_short(step['result'])}")
        lines.append(f"REPLY: {turn['reply'] or '[no final reply: the run ended without an answer]'}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build or inspect review sessions.")
    parser.add_argument("--refresh", action="store_true", help="rebuild the cache")
    parser.add_argument(
        "--source", choices=["langfuse", "export"], default="langfuse"
    )
    parser.add_argument("--show", metavar="SESSION_PREFIX", help="print one session")
    args = parser.parse_args()

    payload = refresh(args.source) if args.refresh else load_cached()
    print(
        f"{payload['session_count']} sessions / {payload['trace_count']} traces "
        f"from {payload['source']}"
        + (f" ({payload['fallback_reason']})" if payload.get("fallback_reason") else "")
    )
    if args.show:
        matches = [
            s for s in payload["sessions"] if s["session_id"].startswith(args.show)
        ]
        for session in matches:
            print(render_text(session))
        if not matches:
            print(f"no session starts with {args.show}")


if __name__ == "__main__":
    main()
