# Experiments

Everything here runs on the 29 public dev tasks with `eval/run.py` and records
into `experiments/scoreboard.csv`. The writeup's rubric wants three things from
this file's output: controlled ablations ("we added X and the dev score went
from A to B"), a cross-model table, and a score-vs-cost curve with plateaus.

## Metrics per run

| Field | Source |
|---|---|
| `score` | mean normalized reward over 29 dev tasks (`results.json`) |
| `score_elec`, `score_mech`, `score_civil`, `score_struct` | per-discipline means (18/23 hidden weighting means electrical + mechanical matter most) |
| `zero_tasks` | tasks scoring 0 (the population the harness is designed to shrink) |
| `contract_fail` | tasks whose deterministic gate failed at the end |
| `gate_fix_saved` | tasks where the gate failed and the gate-fix round recovered the deliverable |
| `in_tokens`, `out_tokens`, `cached_tokens` | from `usage` frames |
| `cost_usd` | tokens × published per-model price |
| `wall_sec` | mean harness runtime per task |
| `calls_per_task` | model calls per task |

## Order of work (each step gates the next)

1. **R0 — smoke on 5 tasks** (2 `checks`, 2 `rubric`, 1 `checks-judge`):
   `data-center-site-model`, `wind-diaphragm-shear`, `footing-settlement-verification`,
   `lv-cable-sizing`, `data-hall-mep-routing`. Proves the harness runs, MCP
   attaches where present, and artifacts export.
2. **R1 — full dev, two models** (rubric criterion 3): Claude Opus 5.5 (target)
   and Gemini 4 Argon; then Gemini 3.8 Flash and GLM 5.2 if budget allows.
3. **R2 — ablations** from the baseline (`ablations.yaml`).
4. **R3 — efficiency sweep**: cap model calls / output tokens / build share and
   find where the score plateaus.
5. **R4 — final artifacts** with the chosen configuration, then the writeup.

## Ablations

| ID | Change vs baseline | Hypothesis | What the writeup shows |
|---|---|---|---|
| A1 | no contract gate (`GATE=off`) | the gate removes zeros, not average score | zero-task count and points difference |
| A2 | no verifier (build only) | reviewer round earns its tokens on rubric tasks | score vs added cost |
| A3 | no survey (plan straight from file map) | survey pays off on document-heavy tasks | per-discipline delta |
| A4 | MCP: register all tools vs selected top-40 | whole-catalog registration costs tokens and score | input tokens, MCP-task scores |
| A5 | `RESULT_CHARS` 4 KB / 16 KB / 32 KB | clipping floor before reasoning degrades | score vs input tokens |
| A6 | build share 0.40 / 0.52 / 0.65 | earlier verification vs more build time | per-task-type delta |
| A7 | effort `high` vs `max` (Claude) | reasoning effort's marginal return | score vs output tokens |
| A8 | one fix round vs two | diminishing returns of the second round | score vs cost |

Each ablation is a config in `experiments/ablations.yaml`; run it with
`--env-file experiments/<id>.env` (variables are read by `serve.py`/`engine.py`)
and append the row to `scoreboard.csv`.

## Failure analysis (for rubric criteria 1 and 2)

For every zero or low-scoring task in the baseline run, classify from
`runs/<run>/attempt1/jobs/dev/<task>/agent/<name>.txt` and the trajectory:

| Code | Failure | Evidence to quote |
|---|---|---|
| F1 | deliverable missing / wrong path | gate report |
| F2 | deliverable unreadable (corrupt container, wrong format) | gate report |
| F3 | numbers copied, not recomputed | verifier gap, grader diff |
| F4 | unit / coordinate error | recomputation vs deliverable |
| F5 | wrong code clause or edition | clause quoted vs governing doc |
| F6 | drawing callout misread | sheet and callout in survey vs deliverable |
| F7 | stopped early / truncated document | trajectory end, file tail |
| F8 | MCP app not used, manual approximation | tool log |
| F9 | timeout or crash | log |

The distribution over F1–F9 is the required "failure breakdown" figure; two
worked examples (one pass, one fail) supply the "engineering reasoning" section.

## Figures

`experiments/make_figures.py` produces:

1. `arch.png` — stage diagram (from the same table as `docs/ARCHITECTURE.md`).
2. `score_cost.png` — dev score vs cost per model/ablation, with the plateau
   marked, for rubric criterion 4.
3. `failures.png` — F1–F9 distribution for the baseline run.

Run `python experiments/make_figures.py --scoreboard experiments/scoreboard.csv`
after filling the scoreboard.
