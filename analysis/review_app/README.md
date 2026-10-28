# Session review app (HW4)

A review interface for open coding Cartwheel conversations. Cartwheel writes one Langfuse trace per user turn; this app groups the traces of a conversation by `cartwheel.session_id` and shows the whole session as one readable timeline.

## Run

From the repository root, with Langfuse running:

```bash
uv run python -m analysis.review_app.sessions --refresh   # pull traces from Langfuse into the cache
uv run python -m analysis.review_app.graph                # cluster sessions for the Map tab
uv run python -m analysis.review_app.server               # http://127.0.0.1:8030/
```

The session cache lives in `analysis/review_app/.cache/` and is not committed; Langfuse stays the canonical trace store. If Langfuse is unreachable, `--refresh` falls back to the Module 1 export `traces/support_traces.json` and the app shows a red banner. Record the reason in `analysis/report/interface_comparison.md`.

Print one session as text, without the UI:

```bash
uv run python -m analysis.review_app.sessions --show 4b23
```

## Tabs

| Tab | Purpose |
|---|---|
| Review | Read a session top to bottom: user message, narration (model text the user never sees), tool call with its result, final reply. Select text and type a note to record the first failure. Use "No failure observed" for a clean session. AI suggestions appear dashed in the margin with Accept and Reject. |
| Map | Sessions clustered by appearance (size, role, tools). Filled marks are reviewed, larger marks are in a batch, squares are cluster representatives. |
| Taxonomy | Candidate and confirmed failure modes with definition, requirement source, boundary, evaluator type, originating notes, examples, and revision history. Changes are made in the chat with the coding agent. |
| Labels | One Pass/Fail judgment per reviewed session and confirmed mode. |
| Progress | Batch coverage, modes first seen per batch (saturation), missing judgments, and the suggestion queue. |

Keyboard: `j` / `k` for the next and previous session in the filtered list.

## Where the data goes

| File under `analysis/state/` | Written by | Content |
|---|---|---|
| `annotations.json` | browser | Open-code notes `{"annotations": [...]}`; edits keep earlier wording in `history`. Local only. |
| `sample_manifest.json` | coding agent | Batch and selection reason per session. |
| `patterns.json` | coding agent | The taxonomy `{"modes": [...], "revisions": [...]}`. |
| `suggestions.json` | agent and browser | Suggestions with `status` pending, accepted, or rejected (with a reason). |
| `graph.json` | `graph.py` | Map coordinates and clusters. |
| `labels/<mode>.jsonl` | browser via server | Append-only judgments, `label` 1 = failure present. |

Each judgment is also written to Langfuse as a numeric score named after the mode. A Fail is stored on the trace of the turn holding the evidence, and a Pass on the session's last turn. Langfuse applies score writes and deletions through a queue, so reconcile it after labeling:

```bash
uv run python -m analysis.review_app.labels --sync --dry-run
uv run python -m analysis.review_app.labels --sync
```

The state directory can be redirected with `CARTWHEEL_ANALYSIS_STATE`, which the tests use.
