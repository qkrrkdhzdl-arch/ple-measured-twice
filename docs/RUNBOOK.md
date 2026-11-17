# Runbook — from this repository to a submitted entry

Everything below runs on a machine with Docker (your PC), not in the workspace
sandbox. Times assume one 8-core machine.

## 0. Accounts and access (one time)

| What | How | Why |
|---|---|---|
| Kaggle identity verification | kaggle.com → Settings → Identity verification → `Verify my account` on a phone | without it the competition cannot be joined or submitted |
| Join the competition | competition page → `Join Hackathon` | required before a Writeup can be submitted |
| Hugging Face access | huggingface.co/datasets/google/physical-engineerings-last-exam → request access → create a read token | the dev set is gated |
| GitHub repo | push this repository (public, Apache-2.0) | Stage 1 requires a repo URL + 40-char commit SHA |

## 1. Local setup (once)

```bash
git clone https://github.com/google-research/ple.git
cd ple
uv tool install harbor==0.20.0 --with websockets
export HF_TOKEN=<your read token>
```

`local.env` (kept outside the entry repository, in the `ple` clone):

```bash
HF_TOKEN=hf_xxx
# one of the two model routes:
MODEL_PROXY_URL=http://localhost:4000     # google-cloud models via Vertex
MODEL_PROXY_KEY=<key you gave start.sh>
# ...or a personal model for local runs only:
# OPENAI_API_KEY=sk-xxx
JUDGE_BASE_URL=https://<openai-compatible>/v1
JUDGE_API_KEY=<key>
JUDGE_MODEL=<judge model, e.g. claude-opus-5-5>
```

For `google-cloud` models locally:

```bash
gcloud auth application-default login
export VERTEXAI_PROJECT=<project> MODEL_PROXY_KEY=$(openssl rand -hex 24)
kit/model_proxy/start.sh &
```

## 2. Smoke test five tasks (about 1–3 hours)

```bash
uv run python eval/run.py ../measured-twice --env-file local.env \
  --tasks data-center-site-model,wind-diaphragm-shear,footing-settlement-verification,lv-cable-sizing,data-hall-mep-routing
uv run python -m kit artifacts ../measured-twice
```

Expect: `CONTRACT CHECK PASSED` in the logs for most tasks; the MCP task
(`data-hall-mep-routing`) shows `[mcp] servers=['apps'] tools=N`.

## 3. Full dev set, two models (one per night)

```bash
cp measured-twice/submission.yaml /tmp/submission.opus.yaml
sed -i 's#claude-opus-5-5#gemini-4-argon#' measured-twice/submission.yaml
uv run python eval/run.py ../measured-twice --env-file local.env --trials 4
```

Record each run in `experiments/scoreboard.csv` (score, per-discipline means,
zero tasks, tokens, cost, wall time). Switch back to the target model before
the final artifacts run: the committed `submission.yaml` must name the Stage 2
model, and `artifacts/manifest.json` must match the submitted harness.

## 4. Ablations

```bash
for id in A1-no-gate A2-no-verifier A4-mcp-all A5-clip-4k A6-build-40; do
  grep -A20 "id: $id" experiments/ablations.yaml | sed -n 's/.*env: {\(.*\)}/\1/p' > /tmp/$id.env
  uv run python eval/run.py ../measured-twice --env-file local.env --env-file /tmp/$id.env \
    --out runs/$id
done
```

## 5. Final artifacts and submission

```bash
uv run python eval/run.py ../measured-twice --env-file local.env     # final artifacts
git -C ../measured-twice add -A
git -C ../measured-twice commit -m "harness and dev artifacts"
uv run python -m kit check ../measured-twice                          # must print ready to submit
git -C ../measured-twice push
git -C ../measured-twice rev-parse HEAD                               # 40-char SHA for the writeup
```

Then: Kaggle → competition → Writeups → New Writeup → paste the SHA at the top,
add the repo under Project Links, attach the 3 figures, keep it under 2,000
words, and submit. **Submit at least 12 hours before 2027-01-05 23:59 UTC.**

## Failure playbook

| Symptom | Meaning | Action |
|---|---|---|
| `kit check` fails at `model` | proxy/key wrong | re-check `MODEL_PROXY_URL`/`KEY`, restart `start.sh` |
| `403` from the model in Stage 2-like runs | key scoped to another model | `submission.yaml` must name the model the key is for |
| `artifacts` review refuses | not committed, LFS, or >512 MiB | commit the folder directly, no LFS, drop `trajectory.json` |
| judge tasks unmeasured | `JUDGE_*` missing or unreachable from containers | set `JUDGE_BASE_URL` to the Docker bridge address |
| task scores 0 with a good-looking file | wrong path or format | read the gate line in the run log — it names the failing path |
