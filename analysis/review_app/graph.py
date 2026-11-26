"""Cluster sessions and project them to 2D for the map view.

Each session becomes a feature vector describing what the conversation looks
like (never whether it failed): size, role, and which tools ran. Features are
standardized, clustered with k-means (k-means++ seeding, several restarts, a
fixed seed), and projected to 2D with PCA. The representative of a cluster is
the session nearest its centroid.

The output ``analysis/state/graph.json`` backs ``GET /api/graph``:

    {"nodes": [{"session_id", "x", "y", "cluster", "distance"}],
     "clusters": [{"id", "size", "representative", "profile"}],
     "features": [...], "k": 8, "seed": 0, "built_at": "..."}

Run it from the repository root:

    uv run python -m analysis.review_app.graph            # k=8
    uv run python -m analysis.review_app.graph --k 6 --features size,role
    uv run python -m analysis.review_app.graph --k 15 --exclude-prefix smoke- --exclude-prefix pilot-
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

import numpy as np

from analysis.helpers import _state
from analysis.review_app import sessions as session_store

ROLES = ("shopper", "merchant", "support")
FEATURE_GROUPS = ("size", "role", "tools", "tool_outcomes")
DEFAULT_GROUPS = ("size", "role", "tools")


def _tool_calls(session: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for t in session["turns"] for s in t["steps"] if s["kind"] == "tool"]


def featurize(
    all_sessions: list[dict[str, Any]], groups: tuple[str, ...] = DEFAULT_GROUPS
) -> tuple[np.ndarray, list[str]]:
    """Return a (sessions x features) matrix and the feature names.

    ``size``: turns, tool calls, distinct tools, characters of user text.
    ``role``: one-hot role. ``tools``: one column per tool name (used or not).
    ``tool_outcomes``: write tool used, any tool error, any empty result.
    """
    tool_names = sorted({c["name"] for s in all_sessions for c in _tool_calls(s)})
    names: list[str] = []
    if "size" in groups:
        names += ["turns", "tool_calls", "distinct_tools", "user_chars"]
    if "role" in groups:
        names += [f"role={r}" for r in ROLES]
    if "tools" in groups:
        names += [f"tool={t}" for t in tool_names]
    if "tool_outcomes" in groups:
        names += ["write_tool", "tool_error", "empty_result"]

    rows = []
    for s in all_sessions:
        calls = _tool_calls(s)
        used = {c["name"] for c in calls}
        badges = set(s["badges"])
        row: list[float] = []
        if "size" in groups:
            row += [
                s["turn_count"],
                len(calls),
                len(used),
                float(np.log1p(sum(len(t["user"]) for t in s["turns"]))),
            ]
        if "role" in groups:
            row += [float(s["role"] == r) for r in ROLES]
        if "tools" in groups:
            row += [float(t in used) for t in tool_names]
        if "tool_outcomes" in groups:
            row += [
                float("write" in badges),
                float(any(b.startswith("error:") for b in badges)),
                float("empty_result" in badges),
            ]
        rows.append(row)
    return np.asarray(rows, dtype=float), names


def standardize(x: np.ndarray) -> np.ndarray:
    std = x.std(axis=0)
    std[std == 0] = 1.0
    return (x - x.mean(axis=0)) / std


def kmeans(
    x: np.ndarray, k: int, seed: int = 0, restarts: int = 10, iters: int = 100
) -> tuple[np.ndarray, np.ndarray]:
    """k-means with k-means++ seeding; return (assignments, centroids)."""
    rng = np.random.default_rng(seed)
    best: tuple[float, np.ndarray, np.ndarray] | None = None
    for _ in range(restarts):
        centroids = [x[rng.integers(len(x))]]
        for _ in range(1, k):
            d2 = np.min(((x[:, None, :] - np.asarray(centroids)[None]) ** 2).sum(-1), axis=1)
            probs = d2 / d2.sum() if d2.sum() > 0 else None
            centroids.append(x[rng.choice(len(x), p=probs)])
        c = np.asarray(centroids)
        for _ in range(iters):
            assign = ((x[:, None, :] - c[None]) ** 2).sum(-1).argmin(axis=1)
            new = np.asarray(
                [x[assign == j].mean(axis=0) if np.any(assign == j) else c[j] for j in range(k)]
            )
            if np.allclose(new, c):
                break
            c = new
        inertia = float(((x - c[assign]) ** 2).sum())
        if best is None or inertia < best[0]:
            best = (inertia, assign, c)
    assert best is not None
    return best[1], best[2]


def pca_2d(x: np.ndarray) -> np.ndarray:
    centered = x - x.mean(axis=0)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    return centered @ vt[:2].T


def _profile(members: list[dict[str, Any]]) -> str:
    """A short plain-language description of a cluster for the legend."""
    roles = {r: sum(m["role"] == r for m in members) for r in ROLES}
    mix = ", ".join(f"{n} {r}" for r, n in sorted(roles.items(), key=lambda kv: -kv[1]) if n)
    turns = np.mean([m["turn_count"] for m in members])
    calls = np.mean([len(_tool_calls(m)) for m in members])
    tools: dict[str, int] = {}
    for m in members:
        for name in {c["name"] for c in _tool_calls(m)}:
            tools[name] = tools.get(name, 0) + 1
    common = [t for t, n in sorted(tools.items(), key=lambda kv: -kv[1]) if n >= len(members) / 2]
    return (
        f"roles: {mix} · avg {turns:.1f} turns · avg {calls:.1f} tool calls · "
        f"tools in at least half: {', '.join(common[:3]) or 'none'}"
    )


def build_graph(
    all_sessions: list[dict[str, Any]],
    k: int = 8,
    seed: int = 0,
    groups: tuple[str, ...] = DEFAULT_GROUPS,
    excluded_prefixes: tuple[str, ...] = (),
) -> dict[str, Any]:
    all_sessions = [
        s for s in all_sessions if not (s.get("scenario_id") or "").startswith(excluded_prefixes or ("\0",))
    ]
    raw, names = featurize(all_sessions, groups)
    x = standardize(raw)
    assign, centroids = kmeans(x, k, seed)
    xy = pca_2d(x)
    distance = np.sqrt(((x - centroids[assign]) ** 2).sum(-1))

    clusters = []
    for j in range(k):
        idx = np.flatnonzero(assign == j)
        if not len(idx):
            continue
        rep = int(idx[distance[idx].argmin()])
        members = [all_sessions[i] for i in idx]
        clusters.append(
            {
                "id": j,
                "size": int(len(idx)),
                "representative": all_sessions[rep]["session_id"],
                "profile": _profile(members),
            }
        )
    nodes = [
        {
            "session_id": s["session_id"],
            "x": round(float(xy[i, 0]), 4),
            "y": round(float(xy[i, 1]), 4),
            "cluster": int(assign[i]),
            "distance": round(float(distance[i]), 4),
        }
        for i, s in enumerate(all_sessions)
    ]
    return {
        "nodes": nodes,
        "clusters": clusters,
        "features": names,
        "feature_groups": list(groups),
        "excluded_prefixes": list(excluded_prefixes),
        "k": k,
        "seed": seed,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the session map (state/graph.json).")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--features",
        default=",".join(DEFAULT_GROUPS),
        help=f"comma-separated groups from {', '.join(FEATURE_GROUPS)}",
    )
    parser.add_argument(
        "--exclude-prefix",
        action="append",
        default=[],
        help="leave sessions whose scenario_id starts with this out of the map (repeatable)",
    )
    args = parser.parse_args()
    groups = tuple(g.strip() for g in args.features.split(",") if g.strip())
    unknown = set(groups) - set(FEATURE_GROUPS)
    if unknown:
        parser.error(f"unknown feature groups: {', '.join(sorted(unknown))}")

    payload = session_store.load_cached()
    graph = build_graph(
        payload["sessions"],
        k=args.k,
        seed=args.seed,
        groups=groups,
        excluded_prefixes=tuple(args.exclude_prefix),
    )
    _state.write_json(_state.state_path("graph.json"), graph)
    print(f"{len(graph['nodes'])} sessions, {len(graph['features'])} features, k={args.k}")
    for c in sorted(graph["clusters"], key=lambda c: -c["size"]):
        print(f"  cluster {c['id']}: {c['size']:3d}  rep {c['representative'][:8]}  {c['profile']}")


if __name__ == "__main__":
    main()
