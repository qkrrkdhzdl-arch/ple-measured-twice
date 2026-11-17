"""measured-twice: a budgeted, contract-driven engineering harness for PLE.

Stages, each with its own clock share:

  intake   (no model)   file map + deliverable contract parsed from the task
  survey   (read-only)  what is in the folder, the method, the traps
  plan     (read-only)  verifiable checklist per deliverable, biggest first
  build    (all tools)  engineering work, largest scoring item first
  gate     (no model)   deterministic checks on the graded paths
  verify   (read-only)  a fresh reviewer recomputes and lists gaps
  fix      (all tools)  gap-driven repairs, bounded by the clock
  close    (no model)   cleanup, final message with paths and key results

Everything is deadline-aware: every stage wraps up before its share of the task
time runs out, a `cancel` frame ends the run with the best summary available,
and the final message is sent even when the model never answers again.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex

import contract
import engine
import mcpbridge
import taskbridge

HERE = os.path.dirname(os.path.abspath(__file__))


def prompt(name: str) -> str:
    with open(os.path.join(HERE, "prompts", f"{name}.md"), encoding="utf-8") as fh:
        return fh.read().strip()


# Clock shares, as the fraction of the task still left when a stage must stop.
# Overridable so ablations can test a different split without touching code.
def share(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


SURVEY_END = share("SURVEY_END", 0.84)
PLAN_END = share("PLAN_END", 0.78)
BUILD_END = share("BUILD_END", 0.26)
VERIFY_END = share("VERIFY_END", 0.14)
FIX_END = share("FIX_END", 0.02)
MAX_FIX_ROUNDS = int(os.environ.get("MAX_FIX_ROUNDS", "2"))
GATE = os.environ.get("GATE", "on") != "off"
SKIP_SURVEY = os.environ.get("SKIP_SURVEY") == "1"
SKIP_PLAN = os.environ.get("SKIP_PLAN") == "1"
SKIP_VERIFY = os.environ.get("SKIP_VERIFY") == "1"
# Tools are clipped per call; these caps protect against a runaway loop.
MAX_BUILD_STEPS = int(os.environ.get("MAX_BUILD_STEPS", "220"))


def tool(name: str, description: str, **params: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {k: {"type": v} for k, v in params.items()},
                "required": list(params),
            },
        },
    }


BASH = tool(
    "bash",
    "Run a bash command in the task container (default 120 s). Returns exit "
    "code and combined stdout/stderr. Prefer short, targeted commands.",
    command="string",
    timeout_sec="integer",
)
READ = tool(
    "read_file",
    "Read a file as text. Large files are clipped in the middle; use "
    "`bash` with sed/head/grep for a specific slice.",
    path="string",
)
WRITE = tool(
    "write_file",
    "Create or overwrite a file with the given text. Parent folders are "
    "created. Use it for deliverables and scratch scripts.",
    path="string",
    content="string",
)
LIST = tool(
    "list_files",
    "List files under a folder with sizes, newest first.",
    path="string",
    depth="integer",
)
GREP = tool(
    "grep",
    "Search text files under a folder for a regular expression (ripgrep-style "
    "output, first 80 matches).",
    pattern="string",
    path="string",
)


class Harness:
    def __init__(self, session):
        self.session = session
        self.engine = engine.Engine(session)
        self.mcp = mcpbridge.MCPBridge(session, limit=int(os.environ.get("MCP_LIMIT", "40")))
        self.mcp_tools: list[dict] = []
        self.contract: list[dict] = []
        self.summary = ""
        self.gate_report = ""

    # ----------------------------------------------------------------- tools
    async def dispatch(self, name: str, args: dict) -> str:
        s = self.session
        if name == "bash":
            result = await s.exec(
                args.get("command", "true"),
                timeout_sec=int(args.get("timeout_sec") or 120),
            )
            out = result.get("output", "")
            code = result.get("exit_code")
            extra = " [output truncated by the evaluator]" if result.get("truncated") else ""
            if result.get("timed_out"):
                extra += " [command timed out]"
            return f"exit={code}{extra}\n{out}"
        if name == "read_file":
            path = args.get("path", "")
            try:
                data = await s.read_file(path)
            except Exception as exc:
                return f"Error: {exc}"
            text = data.decode("utf-8", "replace")
            return f"{len(data)} bytes\n{text}"
        if name == "write_file":
            path = args.get("path", "")
            content = args.get("content", "")
            if path.startswith(("/app/", "/filesystem/")) and "/tmp/" not in path:
                # Scratch scripts belong in /tmp; a deliverable written here is
                # still allowed but flagged in the log for the writeup.
                await s.log(f"[build] write into graded tree: {path}")
            try:
                size = await s.write_file(path, content.encode())
            except Exception as exc:
                return f"Error writing {path}: {exc}"
            return f"wrote {size} bytes to {path}"
        if name == "list_files":
            path = args.get("path") or s.workdir
            depth = int(args.get("depth") or 2)
            code, out = await contract.sh(
                s,
                f"find {shlex.quote(path)} -maxdepth {depth} -printf '%y %8s %p\\n' 2>/dev/null "
                "| head -200",
                timeout=60,
            )
            return out or f"(nothing under {path})"
        if name == "grep":
            pattern = args.get("pattern", "")
            path = args.get("path") or s.workdir
            code, out = await contract.sh(
                s,
                f"grep -rIn -E {shlex.quote(pattern)} {shlex.quote(path)} 2>/dev/null | head -80",
                timeout=120,
            )
            return out or "(no matches)"
        if name == "mcp_search":
            query = args.get("query", "")
            limit = int(args.get("limit") or 10)
            found = self.mcp.search(query, limit)
            self.mcp_tools.extend(self.mcp.register(found))
            self._log_toolset()
            return "Added to your tools:\n" + "\n".join(found)
        full = self.mcp.resolve(name)
        if full:
            return await self.mcp.call(full, args)
        return f"Error: unknown tool {name}"

    def _log_toolset(self):
        self.session.log(
            f"[mcp] {len(self.mcp_tools)} tools exposed: "
            + ", ".join(t["function"]["name"] for t in self.mcp_tools[:12])
        )

    def tools(self, mcp: bool = False) -> list[dict]:
        base = [BASH, READ, WRITE, LIST, GREP]
        if mcp and self.mcp.ready:
            return base + self.mcp_tools + self.mcp.meta_tools()
        return base

    # ---------------------------------------------------------------- phases
    def until(self, fraction: float) -> float:
        total = self.session.time_left()
        if total is None:
            return 0
        return max(0.0, total * fraction)

    async def solve(self) -> str:
        s = self.session
        start_left = s.time_left()
        await s.log(
            f"[intake] {s.workdir} time_left={start_left} model={engine.MODEL} "
            f"effort={engine.EFFORT}"
        )

        # ---- intake: no model yet, free information -------------------------
        listing = await contract.inventory(s)
        self.contract = contract.parse_contract(s.instruction, s.workdir)
        contract_text = json.dumps(self.contract, indent=2, ensure_ascii=False)
        await s.log(f"[intake] contract paths={[c['path'] for c in self.contract]}")
        self.mcp_tools = await self.mcp.start(s.instruction)

        header = (
            f"## Task\n{s.instruction}\n\n"
            f"## Deliverable contract (parsed from the task)\n{contract_text}\n\n"
            f"## Project file map\n{listing[:12000]}\n"
        )

        # ---- survey ---------------------------------------------------------
        survey = ""
        if not SKIP_SURVEY and (s.time_left() is None or s.time_left() > 300):
            survey = await self.engine.run_role(
                name="survey",
                system=prompt("survey"),
                user=header,
                tools=[BASH, READ, LIST, GREP],
                dispatch=self.dispatch,
                deadline_sec=self.until(SURVEY_END),
                max_steps=60,
            )
            await s.log(f"[survey] {len(survey)} chars")

        # ---- plan -----------------------------------------------------------
        plan = ""
        if not SKIP_PLAN and (s.time_left() is None or s.time_left() > 240):
            plan = await self.engine.run_role(
                name="plan",
                system=prompt("plan"),
                user=header + f"\n## Survey\n{survey[:20000]}\n",
                tools=[BASH, READ, LIST],
                dispatch=self.dispatch,
                deadline_sec=self.until(PLAN_END),
                max_steps=30,
            )
            await s.log(f"[plan] {len(plan)} chars")

        # ---- build ----------------------------------------------------------
        build_prompt = (
            header
            + f"\n## Survey\n{survey[:20000]}\n"
            + f"\n## Plan\n{plan[:12000]}\n"
            + (
                "\n## Contract rule\nA deterministic check runs after you stop. It "
                "fails on a missing or empty deliverable, a corrupt container, an "
                "IFC without its header, JSON that does not parse, placeholder "
                "text left in a graded folder, and a short final summary.\n"
            )
        )
        self.summary = await self.engine.run_role(
            name="build",
            system=prompt("worker"),
            user=build_prompt,
            tools=self.tools(mcp=True),
            dispatch=self.dispatch,
            deadline_sec=self.until(BUILD_END),
            max_steps=MAX_BUILD_STEPS,
            final=True,
            reminder=self.build_reminder(),
        )
        await s.log(f"[build] {len(self.summary)} chars, calls={self.engine.calls}")

        # ---- gate: deterministic checks before spending model time ----------
        self.gate_report = await contract.check(s, self.contract, self.summary)
        await s.log(f"[gate] {self.gate_report.splitlines()[0]}")
        if (
            GATE
            and not self.gate_report.startswith("CONTRACT CHECK PASSED")
            and self._time_ok(240)
        ):
            # A missing deliverable is not a judgement call: repair it first,
            # before the reviewer spends time reading what is not there.
            self.summary = (
                await self.engine.run_role(
                    name="gate-fix",
                    system=prompt("worker"),
                    user=(
                        "## Task\n"
                        f"{s.instruction}\n\n## Deterministic contract check\n"
                        f"{self.gate_report}\n\n## Your summary\n{self.summary}\n\n"
                        "Fix every FAIL above. The grader reads only the contract "
                        "paths. Then reply with an updated summary."
                    ),
                    tools=self.tools(mcp=True),
                    dispatch=self.dispatch,
                    deadline_sec=self.until(VERIFY_END),
                    max_steps=80,
                    final=True,
                )
                or self.summary
            )
            self.gate_report = await contract.check(s, self.contract, self.summary)
            await s.log(f"[gate-fix] {self.gate_report.splitlines()[0]}")

        # ---- verify ---------------------------------------------------------
        last_report = ""
        rounds = 0 if SKIP_VERIFY else MAX_FIX_ROUNDS + 1
        for round_no in range(rounds):
            left = s.time_left()
            if left is not None and left < 180:
                break
            review = (
                f"## Task\n{s.instruction}\n\n"
                f"## Plan\n{plan[:8000]}\n\n"
                f"## Deterministic contract check\n{self.gate_report}\n\n"
                f"## Engineer's summary\n{self.summary[:6000]}\n"
            )
            verdict = await self.engine.run_role(
                name=f"verify{round_no}",
                system=prompt("verifier"),
                user=review,
                tools=[BASH, READ, LIST, GREP],
                dispatch=self.dispatch,
                deadline_sec=self.until(VERIFY_END if round_no == 0 else FIX_END),
                max_steps=40,
            )
            await s.log(f"[verify{round_no}] {verdict[:600]}")
            passed = verdict.strip("*_. \r\n\t").upper() in ("", "PASS")
            if passed and self.gate_report.startswith("CONTRACT CHECK PASSED"):
                break
            if round_no == MAX_FIX_ROUNDS:
                break
            if self.gate_report == last_report and passed:
                # Nothing changed and the reviewer is content: stop spending.
                await s.log("[verify] no change after a fix round; stopping")
                break
            last_report = self.gate_report
            if not self._time_ok(engine.WRAP_UP_SEC + 120):
                break
            fix = (
                "## Task\n"
                f"{s.instruction}\n\n## Reviewer's gaps\n{verdict}\n\n"
                f"## Contract check\n{self.gate_report}\n\n"
                "Fix what is listed, then reply with an updated summary. Do not "
                "rewrite work that already passes."
            )
            self.summary = (
                await self.engine.run_role(
                    name=f"fix{round_no}",
                    system=prompt("worker"),
                    user=fix,
                    tools=self.tools(mcp=True),
                    dispatch=self.dispatch,
                    deadline_sec=self.until(FIX_END),
                    max_steps=80,
                    final=True,
                )
                or self.summary
            )
            self.gate_report = await contract.check(s, self.contract, self.summary)
            await s.log(f"[fix{round_no}] {self.gate_report.splitlines()[0]}")

        # ---- close -----------------------------------------------------------
        await self._cleanup()
        listing = await contract.deliverable_listing(s, self.contract)
        final = (
            (self.summary or "No summary was produced.").strip()
            + "\n\n## Graded deliverables\n"
            + (listing or "(none found)")
            + f"\n\n## Contract check\n{self.gate_report}"
        )
        s.final_message = final[:9000]
        meter = self.engine.meter
        await s.log(
            f"[close] calls={meter['calls']} in={meter['input']} out={meter['output']} "
            f"cached={meter['cached']} elapsed_left={s.time_left()}"
        )
        return s.final_message

    def _time_ok(self, need: float) -> bool:
        left = self.session.time_left()
        return left is None or left > need

    def build_reminder(self):
        """Injects the contract every few tool batches instead of every turn."""
        counter = {"n": 0}
        paths = ", ".join(c["path"] for c in self.contract) or "(none parsed)"

        def reminder(messages):
            counter["n"] += 1
            if counter["n"] % 5:
                return (
                    "Keep going. Write deliverables to the contract paths "
                    f"({paths}). Scratch files go in /tmp/m2/."
                )
            left = self.session.time_left()
            mins = "unknown" if left is None else f"{int(left // 60)} min"
            return (
                f"Contract reminder ({mins} left): {paths}. "
                "Check each graded path exists and opens before you stop."
            )

        return reminder

    async def _cleanup(self):
        """Stops scratch processes so the export cannot change under the grader."""
        try:
            await self.session.exec(
                "pkill -f '/tmp/m2/' 2>/dev/null; rm -rf /tmp/m2 2>/dev/null; true",
                timeout_sec=30,
            )
        except Exception:
            pass


async def solve(session):
    harness = Harness(session)
    try:
        return await harness.solve()
    except Exception as exc:  # never lose the deliverables to a crash
        await session.log(f"[fatal] {type(exc).__name__}: {exc}")
        try:
            report = await contract.check(session, harness.contract, harness.summary)
            listing = await contract.deliverable_listing(session, harness.contract)
        except Exception:
            report, listing = "(check unavailable)", ""
        return (
            (harness.summary or "The run ended before the engineer finished.").strip()
            + f"\n\n## Graded deliverables\n{listing}\n\n## Contract check\n{report}"
        )


if __name__ == "__main__":
    taskbridge.run(solve)
