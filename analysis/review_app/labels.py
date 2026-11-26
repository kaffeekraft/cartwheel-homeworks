"""Session-level binary labels: local append-only files plus Langfuse scores.

One judgment per (session, mode). The stored value follows the helpers'
convention, 1 = failure present (Fail), 0 = absent (Pass), so HW5's
``analysis.helpers`` functions read these files unchanged.

A judgment attaches to one trace of the session:

    Fail -> the turn holding the evidence (chosen by the reviewer)
    Pass -> the session's last turn

Files are append-only (``labels/<mode>.jsonl``). Changing a judgment appends a
new record and marks the earlier live record ``superseded_by``; nothing is
deleted, so the label history stays inspectable. In Langfuse the score id is
derived from (mode, trace), so rewriting a judgment on the same trace updates
the score, and moving it to another trace deletes the old score first.

Langfuse ingests scores and deletions through a queue, so a quick change can
request a delete before the old score exists. ``sync`` reconciles Langfuse
with the local live labels; run it after labeling and before committing:

    uv run python -m analysis.review_app.labels --sync --dry-run
    uv run python -m analysis.review_app.labels --sync
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from analysis.helpers import _state

# The committed demonstration mode used by the test suite; never written here.
DEMO_MODES = {"unsupported_policy_claim"}
MODE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def score_id(mode: str, trace_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cartwheel-hw4/{mode}/{trace_id}"))


def _path(mode: str):
    return _state.state_path("labels", f"{mode}.jsonl")


def live_labels(mode: str) -> dict[str, dict[str, Any]]:
    """Return the current record per session for ``mode``."""
    live: dict[str, dict[str, Any]] = {}
    for row in _state.read_jsonl(_path(mode)):
        if row.get("superseded_by") or not row.get("session_id"):
            continue
        live[row["session_id"]] = row
    return live


def record_label(
    mode: str,
    session_id: str,
    trace_id: str,
    label: int,
    comment: str | None = None,
    source: str = "human",
    client: Any | None = None,
) -> dict[str, Any]:
    """Append a judgment, supersede the previous one, and sync Langfuse.

    The local record is written even when Langfuse fails; the returned record
    then carries ``langfuse_ok: False`` and the error, so the UI can warn and
    the judgment can be re-sent later.
    """
    if not MODE_NAME.match(mode) or mode in DEMO_MODES:
        raise ValueError(f"not a writable mode name: {mode!r}")
    if label not in (0, 1):
        raise ValueError("label must be 0 (Pass) or 1 (Fail)")

    path = _path(mode)
    rows = _state.read_jsonl(path)
    previous = next(
        (
            row
            for row in reversed(rows)
            if row.get("session_id") == session_id and not row.get("superseded_by")
        ),
        None,
    )
    ts = datetime.now(timezone.utc).isoformat()
    record: dict[str, Any] = {
        "trace_id": trace_id,
        "session_id": session_id,
        "label": label,
        "source": source,
        "ts": ts,
        "label_id": f"{session_id}#{mode}#{len(rows)}",
        "comment": comment,
        "langfuse_score_id": score_id(mode, trace_id),
    }

    try:
        record["langfuse_ok"] = _sync_score(mode, record, previous, client)
    except Exception as exc:  # network path: keep the local judgment
        record["langfuse_ok"] = False
        record["langfuse_error"] = str(exc)

    if previous is not None:
        for row in rows:
            if row is previous:
                row["superseded_by"] = record["label_id"]
        _state.write_jsonl(path, [*rows, record])
    else:
        _state.append_jsonl(path, record)
    return record


def _sync_score(
    mode: str,
    record: dict[str, Any],
    previous: dict[str, Any] | None,
    client: Any | None,
) -> bool:
    """Write the score to Langfuse. Return False when Langfuse is not configured."""
    from analysis.helpers import langfuse_io

    if client is None:
        if not langfuse_io.is_configured():
            return False
        client = langfuse_io._client()
    if previous is not None and previous.get("trace_id") != record["trace_id"]:
        old = previous.get("langfuse_score_id") or score_id(mode, previous["trace_id"])
        try:
            client.api.score.delete(old)
        except Exception:
            pass  # the old score may never have been written
    client.create_score(
        name=mode,
        value=record["label"],
        trace_id=record["trace_id"],
        score_id=record["langfuse_score_id"],
        data_type="NUMERIC",
        comment=record.get("comment"),
        metadata={"session_id": record["session_id"], "source": record["source"]},
    )
    client.flush()
    return True


# ---------------------------------------------------------------------------
# reconcile Langfuse with the local live labels
# ---------------------------------------------------------------------------


def sync_mode(
    mode: str,
    session_traces: dict[str, list[str]],
    client: Any,
    dry_run: bool = False,
) -> dict[str, int]:
    """Make Langfuse hold exactly one score per live judgment for ``mode``.

    For each session with a live label, scores named ``mode`` on the session's
    other traces (or with another id) are deleted, and a missing live score is
    written again. Returns counts of kept, written, and deleted scores.
    """
    counts = {"kept": 0, "written": 0, "deleted": 0}
    for session_id, record in live_labels(mode).items():
        live_id = record.get("langfuse_score_id") or score_id(mode, record["trace_id"])
        found = False
        for trace_id in session_traces.get(session_id, [record["trace_id"]]):
            for score in client.api.score_v_2.get(trace_id=trace_id).data or []:
                if score.name != mode:
                    continue
                if score.id == live_id and trace_id == record["trace_id"]:
                    found = True
                    counts["kept"] += 1
                    continue
                counts["deleted"] += 1
                if not dry_run:
                    client.api.score.delete(score.id)
        if not found:
            counts["written"] += 1
            if not dry_run:
                _sync_score(mode, {**record, "langfuse_score_id": live_id}, None, client)
    return counts


def main() -> None:
    import argparse

    from dotenv import load_dotenv

    from analysis.helpers import langfuse_io
    from analysis.review_app import sessions

    parser = argparse.ArgumentParser(description="Reconcile Langfuse scores with local labels.")
    parser.add_argument("--sync", action="store_true", required=True)
    parser.add_argument("--dry-run", action="store_true", help="report without changing Langfuse")
    args = parser.parse_args()

    load_dotenv(sessions.ROOT / ".env")
    patterns = _state.read_json(_state.state_path("patterns.json"), default={"modes": []})
    modes = [m["name"] for m in patterns.get("modes", []) if m.get("name") not in DEMO_MODES]
    session_traces = {s["session_id"]: s["trace_ids"] for s in sessions.load_cached()["sessions"]}
    client = langfuse_io._client()
    for mode in modes:
        counts = sync_mode(mode, session_traces, client, dry_run=args.dry_run)
        print(f"{mode}: {counts}{' (dry run)' if args.dry_run else ''}")
    print("Langfuse applies deletions through a queue; rerun --dry-run in a minute to confirm.")


if __name__ == "__main__":
    main()
