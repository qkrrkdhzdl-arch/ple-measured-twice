"""End-to-end test of the harness without Docker or a real model.

Starts the stub model, starts `serve.py` exactly as the evaluator would, and
plays the evaluator over the protocol: send the task, answer `exec` /
`read_file` / `write_file` requests, collect `log` / `usage` / `done`.

    python tests/mock_run.py [--task ple]

It exits 0 only when the harness wrote the graded file and sent `done` with a
final message that names it.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import stub_model  # noqa: E402
import websockets  # noqa: E402

TASK = """# Recompute the total

The project folder holds `data/input.csv` with an `a,b` header. Compute the sum
of column `b` and write it to `/app/out/answer.txt` as a single line
`sum=<value>` with two decimals.

The deliverable `/app/out/answer.txt` is graded. State the method in your final
message.
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def prepare(workdir: str) -> None:
    os.makedirs(os.path.join(workdir, "app/data"), exist_ok=True)
    os.makedirs(os.path.join(workdir, "app/out"), exist_ok=True)
    with open(os.path.join(workdir, "app/data/input.csv"), "w") as fh:
        fh.write("a,b\nfirst,20\nsecond,22\n")


async def play(ws, workdir: str) -> dict:
    """Acts as the evaluator for one task."""
    await ws.send(
        json.dumps(
            {
                "type": "task",
                "protocol": 1,
                "session": "mock",
                "instruction": TASK,
                "workdir": "/app",
                "limits": {
                    "agent_timeout_sec": 900,
                    "exec_timeout_sec": 600,
                    "max_output_bytes": 100000,
                    "max_write_bytes": 10000000,
                },
                "mcp_servers": [],
            }
        )
    )
    logs, usage, done = [], {}, None
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=180)
        except asyncio.TimeoutError:
            break
        msg = json.loads(raw)
        kind = msg.get("type")
        if kind == "log":
            logs.append(msg["text"])
            continue
        if kind == "usage":
            usage[msg["model"]] = usage.get(msg["model"], 0) + msg.get("output_tokens", 0)
            continue
        if kind == "done":
            done = msg
            break
        if kind == "error":
            done = {"error": msg.get("message")}
            break
        if kind == "cancel":
            continue
        # request
        rid = msg.get("id")
        reply = {"type": "result", "id": rid, "ok": False}
        path = msg.get("path")
        try:
            if kind == "exec":
                # Run in the local mirror of the task container; /app maps to
                # <workdir>/app so relative paths behave the same.
                command = msg["command"].replace("/app/", os.path.join(workdir, "app") + "/")
                command = command.replace("/app ", os.path.join(workdir, "app") + " ")
                run_env = dict(os.environ, M2_ROOT=workdir)
                proc = subprocess.run(
                    ["bash", "-c", command],
                    cwd=os.path.join(workdir, "app"),
                    capture_output=True,
                    text=True,
                    timeout=min(int(msg.get("timeout_sec") or 120), 600),
                    env=run_env,
                )
                reply = {
                    "type": "result",
                    "id": rid,
                    "exit_code": proc.returncode,
                    "output": (proc.stdout + proc.stderr)[:100000],
                    "truncated": False,
                    "timed_out": False,
                }
            elif kind in ("read_file", "write_file"):
                local = (path or "").replace("/app/", os.path.join(workdir, "app") + "/")
                if kind == "read_file":
                    data = open(local, "rb").read()
                    reply = {
                        "type": "result",
                        "id": rid,
                        "ok": True,
                        "content_b64": base64.b64encode(data).decode(),
                        "size": len(data),
                        "truncated": False,
                    }
                else:
                    os.makedirs(os.path.dirname(local), exist_ok=True)
                    data = base64.b64decode(msg.get("content_b64") or "")
                    with open(local, "ab" if msg.get("append") else "wb") as fh:
                        fh.write(data)
                    reply = {
                        "type": "result",
                        "id": rid,
                        "ok": True,
                        "size": os.path.getsize(local),
                    }
            else:
                reply = {"type": "result", "id": rid, "ok": False, "error": f"unknown {kind}"}
        except Exception as exc:
            reply = {"type": "result", "id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        await ws.send(json.dumps(reply))
    return {"logs": logs, "usage": usage, "done": done}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the task folder")
    args = ap.parse_args()

    workdir = tempfile.mkdtemp(prefix="m2-task-")
    prepare(workdir)
    model_port = free_port()
    stub_model.serve(model_port)
    port = free_port()
    env = dict(os.environ)
    env.update(
        {
            "PORT": str(port),
            "MODEL_NAME": "stub-model",
            "OPENAI_BASE_URL": f"http://127.0.0.1:{model_port}/v1",
            "OPENAI_API_KEY": "stub",
            "PYTHONPATH": ROOT,
        }
    )
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "serve.py")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 60
    ws = None
    while time.time() < deadline:
        try:
            ws = await websockets.connect(f"ws://127.0.0.1:{port}", max_size=16 * 2**20)
            break
        except OSError:
            await asyncio.sleep(0.5)
    if ws is None:
        proc.kill()
        print((proc.communicate()[0] or "")[-3000:])
        raise SystemExit("server never listened")
    async with ws:
        result = await asyncio.wait_for(play(ws, workdir), timeout=600)
    proc.terminate()
    try:
        out = proc.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        out = ""

    answer = os.path.join(workdir, "app/out/answer.txt")
    ok = os.path.exists(answer) and "sum=42.00" in open(answer).read()
    done = result["done"] or {}
    print("== server output ==")
    print((out or "")[-2000:])
    print("== logs ==")
    for line in result["logs"][-25:]:
        print(" ", line[:220])
    print("== usage ==", result["usage"])
    print("== deliverable ==", open(answer).read().strip() if os.path.exists(answer) else "(missing)")
    print("== final message ==")
    print((done.get("final_message") or done.get("error") or "(none)")[:1200])
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    passed = bool(ok and done.get("final_message") and "answer.txt" in done["final_message"])
    print("RESULT:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
