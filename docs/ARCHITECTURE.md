# Architecture

## The scoring problem, restated

Stage 2 runs the entry on 56 hidden tasks three times, averages the per-task
score, and places the entry on a performance–cost Pareto frontier; only frontier
entries with a mean score ≥ 0.138 can win, and among those the mean score
decides 1st/2nd/3rd. So the design goal is not "cheapest" and not "most
thorough" but **the highest score at a cost that keeps it on the frontier**.

Two facts shape the design:

1. **The hidden set is weighted toward electrical and mechanical work**
   (18 + 23 of 56 tasks). The dev set (9 + 7 of 29) mirrors that, so electrical
   and mechanical failures cost the most.
2. **Frontier models score 0.40–0.48 on this benchmark natively, and only 41% of
   the best configuration's deliverables pass every hard safety check.** The
   headroom is in *deliverables* — producing the exact artifact the grader
   reads — not in raw reasoning.

## Stage design

```
                 ┌───────────────────────────── intake (no model) ─────────────────────────────┐
task text ─────► │ parse deliverable contract (paths/formats)   │   file map (ls/find/du)      │
                 └──────────────────────────────────────────────────────────────────────────────┘
                                    │
                     ┌──────────────▼──────────────┐
                     │ survey  (read-only, 8% clock)│  inputs, governing docs, method, traps
                     └──────────────┬──────────────┘
                     ┌──────────────▼──────────────┐
                     │ plan (read-only, 6% clock)   │  verifiable checklist, biggest item first
                     └──────────────┬──────────────┘
                     ┌──────────────▼──────────────┐
                     │ build (all tools, 52%)       │  engineering work + MCP apps + scratch scripts
                     └──────────────┬──────────────┘
                     ┌──────────────▼──────────────┐
                     │ gate (no model)              │  exists? opens? right container? placeholders?
                     └──────────────┬──────────────┘
                     ┌──────────────▼──────────────┐
                     │ verify (fresh, 12%) → fix    │  independent recomputation, ≤2 gap rounds
                     └──────────────┬──────────────┘
                     ┌──────────────▼──────────────┐
                     │ close (no model)             │  kill scratch, final message with paths
                     └──────────────────────────────┘
```

Clock shares are fractions of the time remaining (`SURVEY_END=0.84`,
`PLAN_END=0.78`, `BUILD_END=0.26`, `VERIFY_END=0.14`, `FIX_END=0.02`), so they
behave the same under any `agent_timeout_sec`, and every one of them is an
environment variable so an ablation can move them without a code change.

## Decisions and their reasons

### 1. Contract before reasoning

`contract.py` scans the instruction for graded paths (`/app/...`,
`/filesystem/...`, plus folder deliverables) and their formats. That parse is
free and it is what the gate checks. The failure it removes: an agent writes an
excellent memo to `report.md` while the grader reads
`/app/files/output/report.md` and scores 0.

### 2. A deterministic gate before the LLM reviewer

Everything machine-checkable is checked without a model: existence, non-zero
size, zip/Office container integrity, the `ISO-10303-21` header for IFC, JSON
parse, placeholder sweep (`TODO`, `TBD`, "Lorem", `<insert`), and a final
message long enough to tell the grader what was delivered. A gate failure goes
straight back to the worker as a repair instruction — the reviewer never spends
tokens reading a file that is not there.

### 3. Selected MCP tools, not the whole catalog

Twelve dev tasks serve project applications as MCP servers; a catalog can hold
hundreds of tools and about 500 KB of schema. Registering all of it is both
expensive and harmful (the model loses the two tools it needs). The bridge
indexes name+description, scores against the task text, registers the top 40,
and exposes `mcp_search` for later needs. Cost of the catalog stays O(1) per
task instead of O(tools) per turn.

### 4. Fresh verifier, and only on the deliverables

The reviewer gets the task, the plan, the gate report, and the summary — not
the worker's reasoning. It is told to recompute decisive numbers rather than
compare against the summary. This catches the failure mode the benchmark
description names explicitly: an agent that "drops unit conversions or quits
early" will also describe its own work as complete.

### 5. Context economy

* Tool results are clipped in the middle at 16 KB, with instructions on how to
  get a narrower slice.
* The conversation is folded into a digest of earlier steps past 300 KB, keeping
  the newest turns; a context-overflow error triggers a harder fold and one
  retry instead of ending the task.
* The contract reminder is injected every fifth tool batch, not every turn.
* Scratch work goes to `/tmp/m2/`, which keeps graded folders clean and lets
  `close` delete it — the exporter copies changed files under deliverable paths,
  so stray files there would be graded.

### 6. Time discipline

Every model call is bounded by the stage's deadline; once a stage is inside its
wrap-up window the model is told to stop investigating and answer, the call is
made with a 240 s cap, and tool calls are not executed from that answer. A
`cancel` frame ends the build and jumps to `close`, so the deliverables already
written are still graded.

## Failure taxonomy this targets

| Failure | Mechanism that addresses it |
|---|---|
| deliverable at the wrong path, or absent | contract parse + gate + gate-fix round |
| plausible but unrecomputed numbers | worker rule "compute with code", verifier recomputation |
| wrong code clause / wrong edition | survey lists governing documents; worker must quote the clause |
| unit and coordinate errors | explicit double-check rule; verifier checks units |
| reading a drawing callout wrong | survey maps sheets; MCP CAD tools surfaced by search |
| stopping early | deadline-aware stages, "never stop early" rule, final message forced |
| context loss on a 6-hour task | compaction digest + file map as source of truth |
| paying for the same dump every turn | clipping + caching marks + selected MCP tools |

## What is deliberately not done

* No provider-hosted tools (forbidden by the rules) and no web access.
* No hidden evaluation of the grader: the harness never reads `artifacts/` or
  any file it produced in an earlier run.
* No hard-coded answers: every number is computed inside the task container from
  the project files, which is also what the reproducibility check verifies.
