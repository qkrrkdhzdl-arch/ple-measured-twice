"""A stub OpenAI-compatible model, so the harness can be tested without Docker.

It answers by role, using the system prompt the harness sends:

* survey   -> a short map that names the deliverable and its checks
* plan     -> a checklist naming the deliverable path
* worker   -> first turn: a `write_file` tool call for a scratch script and the
              deliverable; second turn: a `bash` check; third turn: a summary
* verifier -> `PASS`
* fix      -> a corrected summary

Run: python tests/stub_model.py --port 8899
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"worker_turns": 0, "verifier_turns": 0, "requests": 0}


def reply(content: str | None, tool_calls=None, finish="stop") -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish = "tool_calls"
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 0,
        "model": "stub-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 180, "total_tokens": 1380},
    }


def call(name: str, args: dict) -> dict:
    return {
        "id": f"call_{name}_{STATE['requests']}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def decide(messages: list[dict]) -> dict:
    STATE["requests"] += 1
    system = messages[0].get("content") or ""
    last = messages[-1]
    role_text = json.dumps(last.get("content"))[:2000]
    tools_seen = {m.get("role") for m in messages}

    if "first engineer to open this project folder" in system:
        return reply(
            "## 1. Deliverable contract\n- /app/out/answer.txt (text): the answer, "
            "one line per item.\n\n## 2. Inputs\n- input.csv: two columns.\n\n"
            "## 3. Method\nSum column b.\n\n## 4. Traps\nUnits.\n\n"
            "## 5. Verification\nRecompute the sum."
        )
    if "Turn the survey map into an execution plan" in system:
        return reply(
            "1. Deliverable: /app/out/answer.txt with `sum=<value>`.\n"
            "2. Steps: read input.csv, sum b, write answer.\n"
            "3. Checks: recompute the sum.\n4. Risks: none."
        )
    if "licensed senior engineer doing a real project task" in system:
        STATE["worker_turns"] += 1
        if STATE["worker_turns"] == 1:
            return reply(
                "Reading the input and writing the deliverable.",
                tool_calls=[
                    call(
                        "write_file",
                        {
                            "path": "/tmp/m2/calc.py",
                            "content": (
                                "import csv\n"
                                "import os\nroot=os.environ.get('M2_ROOT','')\n"
                                "rows=list(csv.DictReader(open(root+'/app/data/input.csv')))\n"
                                "total=sum(float(r['b']) for r in rows)\n"
                                "root=__import__('os').environ.get('M2_ROOT','')\n"
                                "open(root+'/app/out/answer.txt','w').write(f'sum={total:.2f}\\n')\n"
                                "print(total)\n"
                            ),
                        },
                    ),
                    call("bash", {"command": "python3 /tmp/m2/calc.py && cat /app/out/answer.txt"}),
                ],
            )
        if STATE["worker_turns"] == 2:
            return reply(
                "Checking the deliverable.",
                tool_calls=[call("bash", {"command": "ls -l /app/out && cat /app/out/answer.txt"})],
            )
        return reply(
            "Wrote /app/out/answer.txt with `sum=42.00`, computed by /tmp/m2/calc.py "
            "from /app/data/input.csv (two rows: 20 and 22)."
        )
    if "You review another engineer's finished work" in system:
        STATE["verifier_turns"] += 1
        if "Reviewer's gaps" in role_text:
            return reply("PASS")
        return reply("PASS")
    if "gaps" in role_text and "worker" not in system:
        return reply("Fixed summary: deliverable rewritten.")
    return reply("OK")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        payload = decide(body.get("messages") or [])
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence
        pass


def serve(port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--write-port", default=None)
    args = ap.parse_args()
    server = serve(args.port)
    if args.write_port:
        with open(args.write_port, "w") as fh:
            fh.write(str(server.server_address[1]))
    print(f"stub model on http://127.0.0.1:{server.server_address[1]}/v1", flush=True)
    threading.Event().wait()
