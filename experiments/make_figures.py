"""Builds the three figures the Stage 1 writeup requires.

    python experiments/make_figures.py

1. `experiments/figures/arch.png`      architecture diagram (always drawn)
2. `experiments/figures/score_cost.png` dev score vs cost, with the plateau
3. `experiments/figures/failures.png`   F1-F9 failure counts for a run

Figures 2 and 3 need data: fill `experiments/scoreboard.csv` from runs and pass
`--failures experiments/failures.json` (counts per code) once the baseline run
is classified. With no data the script still draws the empty axes, so the
writeup can be laid out before the runs finish.
"""

from __future__ import annotations

import argparse
import csv
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "figures")

STAGES = [
    ("intake\nno model", "contract paths\n+ file map"),
    ("survey\nread-only", "inputs, codes,\ntraps"),
    ("plan\nread-only", "verifiable\nchecklist"),
    ("build\nall tools", "deliverables\n+ MCP apps"),
    ("gate\nno model", "exists/opens/\nformat"),
    ("verify\nfresh", "recompute,\nlist gaps"),
    ("fix\nbounded", "repair gaps\n≤2 rounds"),
    ("close", "cleanup +\nfinal message"),
]
FAILURES = {
    "F1": "missing / wrong path",
    "F2": "unreadable deliverable",
    "F3": "numbers not recomputed",
    "F4": "unit / coordinate error",
    "F5": "wrong code clause",
    "F6": "drawing callout misread",
    "F7": "stopped early",
    "F8": "MCP app unused",
    "F9": "timeout / crash",
}


def arch() -> str:
    fig, ax = plt.subplots(figsize=(14, 3.4))
    ax.set_xlim(0, 8)
    ax.set_ylim(0, 2)
    ax.axis("off")
    for i, (title, body) in enumerate(STAGES):
        x = i + 0.06
        shade = "#dbe7f3" if "model" not in title and "no model" not in title else "#eef2f6"
        if "no model" in title or title.startswith("close"):
            shade = "#e8f3e8"
        box = FancyBboxPatch(
            (x, 0.6), 0.88, 0.9, boxstyle="round,pad=0.02,rounding_size=0.06",
            linewidth=1.0, edgecolor="#5b6b7b", facecolor=shade,
        )
        ax.add_patch(box)
        ax.text(x + 0.44, 1.28, title, ha="center", va="center", fontsize=10, weight="bold")
        ax.text(x + 0.44, 0.92, body, ha="center", va="center", fontsize=7.5, color="#33414f")
        if i:
            ax.annotate(
                "", xy=(x, 1.05), xytext=(x - 0.06, 1.05),
                arrowprops=dict(arrowstyle="-|>", color="#5b6b7b", lw=1.1),
            )
    ax.text(
        0.06, 0.3,
        "clock shares (fraction of time left): survey 0.84 · plan 0.78 · build 0.26 · "
        "verify 0.14 · fix 0.02",
        fontsize=8, color="#33414f",
    )
    ax.text(
        0.06, 0.08,
        "selected MCP tools (top-k by task text + mcp_search) · middle-clipped tool results · "
        "conversation digest · usage frames per call",
        fontsize=8, color="#33414f",
    )
    path = os.path.join(OUT, "arch.png")
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return path


def score_cost(rows: list[dict]) -> str:
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    pts = []
    for r in rows:
        try:
            cost = float(r.get("cost_usd") or 0)
            score = float(r.get("score") or 0)
        except ValueError:
            continue
        if cost <= 0:
            continue
        pts.append((cost, score, r.get("run_id", "")))
    if pts:
        for cost, score, label in pts:
            ax.scatter(cost, score, s=46, color="#2b6cb0")
            ax.annotate(label, (cost, score), fontsize=6.5, xytext=(3, 3), textcoords="offset points")
        frontier = []
        best = -1
        for cost, score, _ in sorted(pts):
            if score > best:
                frontier.append((cost, score))
                best = score
        if frontier:
            ax.step(
                [p[0] for p in frontier], [p[1] for p in frontier],
                where="post", color="#c05621", lw=1.2, label="performance-cost frontier",
            )
            ax.legend(fontsize=8)
    else:
        ax.text(0.5, 0.5, "scoreboard empty - run the dev set", ha="center", va="center",
                transform=ax.transAxes, fontsize=9, color="#888")
    ax.axhline(0.138, color="#888", ls="--", lw=0.8)
    ax.text(0.01, 0.145, "Stage 2 prize threshold 0.138", fontsize=7, color="#666",
            transform=ax.get_yaxis_transform())
    ax.set_xlabel("cost per dev-set run (USD)")
    ax.set_ylabel("dev score (mean of 29 tasks)")
    ax.set_title("Score vs cost on the public dev set")
    ax.grid(alpha=0.25)
    path = os.path.join(OUT, "score_cost.png")
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return path


def failures(counts: dict[str, int]) -> str:
    labels = [f"{k}\n{FAILURES.get(k, '')}" for k in FAILURES]
    values = [counts.get(k, 0) for k in FAILURES]
    fig, ax = plt.subplots(figsize=(8.2, 3.6))
    ax.bar(range(len(values)), values, color="#9b2c2c")
    ax.set_xticks(range(len(values)))
    ax.set_xticklabels(labels, fontsize=6.5)
    ax.set_ylabel("dev tasks")
    ax.set_title("Failure breakdown on the 29 dev tasks (baseline run)")
    if not any(values):
        ax.text(0.5, 0.6, "classify the baseline run first", ha="center",
                transform=ax.transAxes, fontsize=9, color="#888")
    path = os.path.join(OUT, "failures.png")
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scoreboard", default=os.path.join(HERE, "scoreboard.csv"))
    ap.add_argument("--failures", default=None)
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    rows = []
    if os.path.exists(args.scoreboard):
        with open(args.scoreboard, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("run_id")]
    counts = {}
    if args.failures and os.path.exists(args.failures):
        with open(args.failures, encoding="utf-8") as fh:
            counts = json.load(fh)

    for path in (arch(), score_cost(rows), failures(counts)):
        print("wrote", path)


if __name__ == "__main__":
    main()
