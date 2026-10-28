"""Offline tests for the HW4 review app's session builder.

The traces below mimic the Langfuse API shape: one trace per user turn, with
session metadata under ``metadata.attributes`` and each GENERATION repeating
the system prompt and history.
"""

from __future__ import annotations

from analysis.helpers.normalization import normalize_trace
from analysis.review_app.sessions import build_sessions, build_turn

SYSTEM = "You are Cartwheel's support assistant."


def _msg(role: str, **part: object) -> dict:
    return {"role": role, "parts": [part]}


def _trace(
    trace_id: str,
    session_id: str,
    scenario_id: str,
    timestamp: str,
    user: str,
    reply: str,
    tool: tuple[str, dict, object] | None = None,
) -> dict:
    """A raw Langfuse-style trace with an optional single tool call."""
    history = [_msg("system", type="text", content=SYSTEM), _msg("user", type="text", content=user)]
    observations = []
    if tool is not None:
        name, arguments, result = tool
        observations += [
            {
                "id": f"{trace_id}-g1",
                "type": "GENERATION",
                "name": "openai.response",
                "startTime": f"{timestamp[:-1]}1Z",
                "input": {"messages": history},
                "output": [
                    _msg("assistant", type="text", content="I'll check that now."),
                    _msg("assistant", type="tool_call", name=name, arguments=arguments),
                ],
            },
            {
                "id": f"{trace_id}-t1",
                "type": "TOOL",
                "name": name,
                "startTime": f"{timestamp[:-1]}2Z",
                "input": repr(arguments),
                "output": repr(result),
            },
        ]
    observations.append(
        {
            "id": f"{trace_id}-g2",
            "type": "GENERATION",
            "name": "openai.response",
            "startTime": f"{timestamp[:-1]}3Z",
            "input": {"messages": history},
            "output": [_msg("assistant", type="text", content=reply)],
        }
    )
    return {
        "id": trace_id,
        "timestamp": timestamp,
        "input": [_msg("user", type="text", content=user)],
        "output": [_msg("assistant", type="text", content=reply)],
        "metadata": {
            "attributes": {
                "cartwheel.session_id": session_id,
                "cartwheel.scenario_id": scenario_id,
                "cartwheel.user_role": "shopper",
                "cartwheel.user_id": "23",
            }
        },
        "observations": observations,
    }


def _normalized(*raw: dict) -> list[dict]:
    return [normalize_trace(r) for r in raw]


def test_turn_separates_narration_tool_and_reply() -> None:
    raw = _trace(
        "t1", "s1", "sc1", "2026-10-20T10:00:00Z", "Where is order 7?", "It shipped.",
        tool=("get_order", {"order_id": 7}, {"ok": True, "order": {"status": "shipped"}}),
    )
    turn = build_turn(normalize_trace(raw))

    assert turn["user"] == "Where is order 7?"
    assert turn["reply"] == "It shipped."
    assert [s["kind"] for s in turn["steps"]] == ["narration", "tool"]
    assert turn["steps"][0]["text"] == "I'll check that now."
    tool = turn["steps"][1]
    assert tool["arguments"] == {"order_id": 7}
    assert tool["result"]["order"]["status"] == "shipped"
    # The final reply appears once, as the reply, not as narration.
    assert all(s.get("text") != "It shipped." for s in turn["steps"])


def test_sessions_group_by_session_id_not_scenario_id() -> None:
    # Two runs of the same scenario are two sessions; turns sort by time.
    traces = _normalized(
        _trace("b", "s1", "sc1", "2026-10-20T10:05:00Z", "second", "r2"),
        _trace("a", "s1", "sc1", "2026-10-20T10:00:00Z", "first", "r1"),
        _trace("c", "s2", "sc1", "2026-10-20T11:00:00Z", "rerun", "r3"),
    )
    sessions = build_sessions(traces)

    assert [s["session_id"] for s in sessions] == ["s1", "s2"]
    s1 = sessions[0]
    assert s1["trace_ids"] == ["a", "b"]
    assert [t["user"] for t in s1["turns"]] == ["first", "second"]
    assert s1["turn_count"] == 2 and "multi_turn" in s1["badges"]
    assert s1["system_prompt"] == SYSTEM
    assert s1["role"] == "shopper" and s1["user_id"] == "23"


def test_tool_badges_flag_writes_errors_and_empty_results() -> None:
    traces = _normalized(
        _trace(
            "t1", "s1", "sc1", "2026-10-20T10:00:00Z", "refund", "Queued.",
            tool=("issue_refund", {"order_id": 1}, {"ok": False, "error": "permission_denied"}),
        ),
        _trace(
            "t2", "s2", "sc2", "2026-10-20T11:00:00Z", "pottery", "None found.",
            tool=("search_products", {"query": "x"}, {"ok": True, "products": [], "count": 0}),
        ),
        _trace("t3", "s3", "sc3", "2026-10-20T12:00:00Z", "hi", "Hello."),
    )
    by_id = {s["session_id"]: s for s in build_sessions(traces)}

    assert by_id["s1"]["turns"][0]["steps"][1]["badges"] == ["write", "error:permission_denied"]
    assert "empty_result" in by_id["s2"]["badges"]
    assert by_id["s3"]["badges"] == ["no_tools"]


def test_turn_without_final_reply_is_flagged() -> None:
    raw = _trace(
        "t1", "s1", "sc1", "2026-10-20T10:00:00Z", "pottery", "unused",
        tool=("search_products", {"query": "x"}, {"ok": True, "products": [], "count": 0}),
    )
    raw["output"] = None
    raw["observations"].pop()  # no final GENERATION: the run stopped mid-loop
    session = build_sessions(_normalized(raw))[0]

    assert session["turns"][0]["reply"] == ""
    assert "no_reply" in session["badges"]


def test_traces_without_session_are_skipped() -> None:
    raw = _trace("t1", "s1", "sc1", "2026-10-20T10:00:00Z", "hi", "Hello.")
    raw["metadata"]["attributes"].pop("cartwheel.session_id")
    assert build_sessions(_normalized(raw)) == []


# ----- labels: append-only history, supersede on change, Langfuse sync -----


class _FakeLangfuse:
    def __init__(self) -> None:
        self.scores: dict[str, dict] = {}
        self.deleted: list[str] = []
        outer = self

        class _Score:
            def delete(self, score_id: str) -> None:
                outer.deleted.append(score_id)
                outer.scores.pop(score_id, None)

        class _Api:
            score = _Score()

        self.api = _Api()

    def create_score(self, *, score_id: str, **kwargs: object) -> None:
        self.scores[score_id] = kwargs

    def flush(self) -> None:
        pass


def test_label_change_supersedes_and_moves_the_score(tmp_path, monkeypatch) -> None:
    from analysis.helpers import _state
    from analysis.review_app import labels

    monkeypatch.setenv("CARTWHEEL_ANALYSIS_STATE", str(tmp_path))
    lf = _FakeLangfuse()

    first = labels.record_label("unexplained_escalation", "s1", "turn1", 1, "ticket without explanation", client=lf)
    second = labels.record_label("unexplained_escalation", "s1", "turn3", 0, client=lf)

    rows = _state.read_jsonl(tmp_path / "labels" / "unexplained_escalation.jsonl")
    assert len(rows) == 2  # nothing deleted
    assert rows[0]["superseded_by"] == second["label_id"]
    assert labels.live_labels("unexplained_escalation") == {"s1": rows[1]}
    # The Fail score on turn 1 was removed; the Pass score sits on turn 3.
    assert lf.deleted == [first["langfuse_score_id"]]
    assert list(lf.scores) == [second["langfuse_score_id"]]
    assert lf.scores[second["langfuse_score_id"]]["value"] == 0
    assert second["langfuse_ok"] is True


def test_label_kept_locally_when_langfuse_fails(tmp_path, monkeypatch) -> None:
    from analysis.review_app import labels

    monkeypatch.setenv("CARTWHEEL_ANALYSIS_STATE", str(tmp_path))

    class _Down(_FakeLangfuse):
        def create_score(self, **kwargs: object) -> None:
            raise ConnectionError("langfuse down")

    record = labels.record_label("unexplained_escalation", "s1", "turn1", 1, client=_Down())
    assert record["langfuse_ok"] is False and "langfuse down" in record["langfuse_error"]
    assert labels.live_labels("unexplained_escalation")["s1"]["label"] == 1


def test_demo_mode_and_bad_names_are_refused(tmp_path, monkeypatch) -> None:
    import pytest

    from analysis.review_app import labels

    monkeypatch.setenv("CARTWHEEL_ANALYSIS_STATE", str(tmp_path))
    for mode in ("unsupported_policy_claim", "Bad Name"):
        with pytest.raises(ValueError):
            labels.record_label(mode, "s1", "t1", 1, client=_FakeLangfuse())


# ----- map: deterministic clusters over appearance features -----


def test_graph_is_deterministic_and_covers_every_session() -> None:
    from analysis.review_app.graph import build_graph

    traces = []
    for i in range(12):
        tool = ("get_order", {"order_id": i}, {"ok": True}) if i % 2 else None
        traces.append(_trace(f"t{i}", f"s{i}", f"sc{i}", f"2026-10-20T10:{i:02d}:00Z", "q" * (i + 1), "r", tool=tool))
    sessions = build_sessions(_normalized(*traces))

    a = build_graph(sessions, k=3, seed=0)
    b = build_graph(sessions, k=3, seed=0)
    assert a["nodes"] == b["nodes"]
    assert {n["session_id"] for n in a["nodes"]} == {s["session_id"] for s in sessions}
    reps = {c["representative"] for c in a["clusters"]}
    assert len(reps) == len(a["clusters"]) and sum(c["size"] for c in a["clusters"]) == 12


def test_sync_deletes_stale_scores_and_rewrites_missing(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from analysis.review_app import labels

    monkeypatch.setenv("CARTWHEEL_ANALYSIS_STATE", str(tmp_path))
    lf = _FakeLangfuse()
    labels.record_label("m", "s1", "turn1", 1, client=lf)
    live = labels.record_label("m", "s1", "turn3", 0, client=lf)
    # Simulate the queue race: the turn-1 score survived, the turn-3 score never landed.
    stale = labels.score_id("m", "turn1")
    by_trace = {"turn1": [SimpleNamespace(name="m", id=stale)], "turn2": [], "turn3": []}
    lf.api.score_v_2 = SimpleNamespace(get=lambda trace_id: SimpleNamespace(data=by_trace[trace_id]))
    lf.scores.clear(); lf.deleted.clear()

    counts = labels.sync_mode("m", {"s1": ["turn1", "turn2", "turn3"]}, lf)

    assert counts == {"kept": 0, "written": 1, "deleted": 1}
    assert lf.deleted == [stale]
    assert list(lf.scores) == [live["langfuse_score_id"]]
