# Writeup outline (Stage 1, ≤2,000 words, 100 points)

Stage 1 is a qualification gate, but ties on Stage 2 score break on it first, so
it is written to the rubric's five criteria in that order. Headline finding near
the top, three required figures, GitHub URL and 40-character commit SHA in
`Project Links`, model and judge documented.

## Header (before the body)

* Repo URL, commit SHA (`git rev-parse HEAD`)
* Model: `google-cloud/…`; local dev model and judge model used to produce
  `artifacts/`

## 1. Insight & failure analysis — 25 pts

* Headline: *on PLE, most lost points are delivery failures, not reasoning
  failures; a contract-first harness with a deterministic gate moved N of 29
  dev tasks from 0 to positive.*
* Table: failure taxonomy F1–F9 with counts from the baseline run.
* Three ablations with numbers: gate on/off, verifier on/off, MCP selection
  vs full catalog.
* What failed and was abandoned (e.g. a self-consistency voting scheme that
  doubled cost for +0.01).

## 2. Engineering reasoning — 25 pts

* One `checks-judge` task walked through end to end: the licensed engineer's
  workflow (governing clause → calculation → check), the agent's trajectory,
  where it held, where it shortcut, which artifact decided pass/fail.
* One `rubric` task: what the reviewer wanted that the summary did not show.

## 3. Cross-model generalizability — 15 pts

* 29 dev tasks × at least two model families; table of score, tokens, cost.
* Which changes help every model (contract gate, clipping) and which help only
  the strong ones (multi-round verification).

## 4. Efficiency & scaling — 20 pts

* `score_cost.png`: dev score vs cost with plateau and remaining headroom.
* Where the money goes per task (survey vs build vs verify) and what was cut.

## 5. Clarity & visuals — 15 pts

* `arch.png`, `score_cost.png`, `failures.png`; short sentences, no jargon
  without a gloss for a civil/mechanical/electrical outsider.

## Checklist before submitting

- [ ] ≤ 2,000 words
- [ ] cover image + 3 figures in the Media Gallery
- [ ] `Project Links`: repo URL + 40-char SHA at the top of the writeup
- [ ] commit contains `submission.yaml`, `serve.py`, `harness.py`/modules,
      Apache-2.0 `LICENSE`, unpacked `artifacts/` (≤512 MiB, ≤10,000 files)
- [ ] `kit check` and `kit artifacts` pass
- [ ] if repo is private: `ple-submission` GitHub App installed; public at close
- [ ] judge model stated if not a frontier model
