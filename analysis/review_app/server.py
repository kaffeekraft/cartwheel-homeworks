"""Session-based review server for HW4.

Extends the reference server (``analysis/server.py``): the same file-backed
JSON API over the analysis state directory, with the review unit changed from
a single trace to a conversation. The app is served from ``review_app/ui/``.

Retained from the reference:

    GET/POST /api/samples, /api/patterns, /api/suggestions; GET /api/graph

Changed or added:

    GET      /api/sessions      cached sessions built by ``sessions.py``
    GET/POST /api/annotations   open-code notes as {"annotations": [...]},
                                stored locally only (no Langfuse sync: notes
                                carry no binary label)
    GET/POST /api/manifest      ``sample_manifest.json``: batch per session
    GET      /api/labels        live judgment per mode and session
    POST     /api/labels        one judgment {mode, session_id, trace_id,
                                label, comment}: appended to
                                ``labels/<mode>.jsonl`` and written to Langfuse

The state directory is ``analysis/state/`` unless ``CARTWHEEL_ANALYSIS_STATE``
points elsewhere (the same override the helpers and tests use).

Run it from the repository root:

    uv run python -m analysis.review_app.server               # :8030
    uv run python -m analysis.review_app.server --port 8031

Trace links point at ``--langfuse-ui`` (default ``http://localhost:3000``),
the address the browser on the host uses, which differs from the container's
``LANGFUSE_HOST``.
"""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from analysis import server as reference
from analysis.helpers import _state
from analysis.review_app import labels, sessions

UI_DIR = Path(__file__).resolve().parent / "ui"

# Route -> (state file, default document). Resolved per request so the state
# directory override applies.
API_FILES: dict[str, tuple[str, Any]] = {
    "/api/samples": ("samples.json", []),
    "/api/annotations": ("annotations.json", {"annotations": []}),
    "/api/graph": ("graph.json", {"nodes": [], "clusters": []}),
    "/api/patterns": ("patterns.json", {"modes": []}),
    "/api/suggestions": ("suggestions.json", []),
    "/api/manifest": ("sample_manifest.json", {"unit": "session", "sessions": {}}),
    "/api/label_suggestions": ("label_suggestions.json", {}),
}
# graph.json is written by graph.py; label_suggestions.json by the coding agent.
READ_ONLY = {"/api/graph", "/api/label_suggestions"}

# Set in ``main`` from ``--langfuse-ui``.
LANGFUSE_UI = "http://localhost:3000"


def _sessions_payload() -> dict[str, Any]:
    """Return the session cache with trace links rewritten for the browser."""
    payload = sessions.load_cached()
    stored = payload.get("langfuse_trace_url")
    payload["trace_url_base"] = (
        f"{LANGFUSE_UI.rstrip('/')}{urlparse(stored).path}" if stored else None
    )
    return payload


def _labels_payload() -> dict[str, dict[str, Any]]:
    """Live judgments for every mode in the taxonomy (demo modes excluded)."""
    patterns = reference._read_json(_state.state_path("patterns.json"), {"modes": []})
    modes = [m.get("name") for m in patterns.get("modes", []) if isinstance(m, dict)]
    return {
        mode: labels.live_labels(mode)
        for mode in modes
        if mode and mode not in labels.DEMO_MODES
    }


class SessionReviewHandler(reference.ReviewHandler):
    """Serves the session review app; reuses the reference JSON helpers."""

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_file(UI_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/api/sessions":
            self._send_json(_sessions_payload())
            return
        if path == "/api/labels":
            self._send_json(_labels_payload())
            return
        if path in API_FILES:
            name, default = API_FILES[path]
            self._send_json(reference._read_json(_state.state_path(name), default))
            return
        self._send_json({"error": f"unknown path: {path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != "/api/labels" and (path not in API_FILES or path in READ_ONLY):
            self._send_json({"error": f"cannot POST to {path}"}, status=404)
            return
        data = self._read_body()
        if data is None:
            self._send_json({"error": "expected a JSON body"}, status=400)
            return
        if path == "/api/labels":
            self._post_label(data)
            return
        reference._write_json(_state.state_path(API_FILES[path][0]), data)
        self._send_json({"ok": True, "count": reference._count(data)})

    def _post_label(self, data: Any) -> None:
        try:
            record = labels.record_label(
                mode=str(data["mode"]),
                session_id=str(data["session_id"]),
                trace_id=str(data["trace_id"]),
                label=int(data["label"]),
                comment=data.get("comment"),
                source=str(data.get("source") or "human"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json({"ok": True, "record": record})


def main() -> None:
    global LANGFUSE_UI
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--langfuse-ui", default=LANGFUSE_UI)
    args = parser.parse_args()
    LANGFUSE_UI = args.langfuse_ui

    from dotenv import load_dotenv

    load_dotenv(sessions.ROOT / ".env")  # Langfuse keys for score writes
    payload = sessions.load_cached()
    print(
        f"{payload['session_count']} sessions / {payload['trace_count']} traces "
        f"from {payload['source']} (built {payload['built_at']})"
    )
    if payload.get("fallback_reason"):
        print(f"fallback: {payload['fallback_reason']}")

    server = ThreadingHTTPServer((args.host, args.port), SessionReviewHandler)
    print(f"review app on http://{args.host}:{args.port}/")
    print(f"state in {_state.state_root()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
