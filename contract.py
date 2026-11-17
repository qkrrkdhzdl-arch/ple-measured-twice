"""Deliverable contract: the part of a PLE task that is not a judgement call.

Almost every PLE task names the file (or folder) it grades and the format it
must be in. Failures that cost full points are usually not subtle engineering
errors: the agent writes a plausible report to the wrong path, leaves a
placeholder in a table, saves a CSV where the grader expects an IFC, or sends
`done` before the file exists. Those are machine-checkable, so this module
checks them before any model time is spent on review.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex

# Paths that appear in PLE instructions: the container roots used by the
# benchmark plus whatever looks like a file the grader reads.
PATH_RE = re.compile(
    r"(/(?:app|filesystem|mnt|data|workspace|home|tmp)(?:/[\w@.+~\-]+)+\.[A-Za-z0-9]{1,8})"
)
FORMATS = {
    "ifc": "IFC4 building model",
    "xlsx": "Excel workbook",
    "docx": "Word document",
    "pdf": "PDF",
    "zip": "archive of the deliverable set",
    "py": "Python source",
    "csv": "CSV table",
    "json": "JSON document",
    "md": "Markdown memo",
    "txt": "text file",
    "yaml": "YAML",
    "pptx": "PowerPoint deck",
}
PLACEHOLDERS = (
    "TODO",
    "TBD",
    "Lorem ipsum",
    "XXX",
    "<insert",
    "[insert",
    "PLACEHOLDER",
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


async def sh(session, command: str, timeout: int = 120) -> tuple[int, str]:
    result = await session.exec(command, timeout_sec=timeout)
    return result.get("exit_code", 1), result.get("output", "")


async def inventory(session) -> str:
    """A cheap file map: what is in the project folder, before reading any of it."""
    workdir = session.workdir
    command = (
        f"cd {shlex.quote(workdir)} && "
        "echo '## top level' && ls -la && "
        "echo '## tree (depth 3, dirs first)' && "
        "find . -maxdepth 3 -mindepth 1 -printf '%y %10s %p\\n' 2>/dev/null | "
        "sort -k3 | head -250 && "
        "echo '## by extension' && "
        "find . -maxdepth 4 -type f -printf '%f\\n' 2>/dev/null | "
        "sed 's/.*\\.//' | sort | uniq -c | sort -rn | head -25 && "
        "echo '## size' && du -sh . 2>/dev/null"
    )
    code, out = await sh(session, command, timeout=180)
    return out if code == 0 else f"(inventory failed: {out[:2000]})"


def parse_contract(instruction: str, workdir: str) -> list[dict]:
    """Extracts explicitly named deliverables from the task instruction."""
    found: dict[str, dict] = {}
    for match in PATH_RE.finditer(instruction):
        path = match.group(1).rstrip(".,);:")
        ext = path.rsplit(".", 1)[-1].lower()
        if ext not in FORMATS:
            continue
        if path.rstrip("/").startswith(("//", "/tmp/")):
            continue
        found.setdefault(
            path,
            {
                "path": path,
                "format": FORMATS[ext],
                "kind": "zip" if ext == "zip" else ("dir" if path.endswith("/") else "file"),
                "source": "path in the task text",
            },
        )
    # A folder deliverable, e.g. "the /app/files folder is graded"
    for match in re.finditer(r"(/(?:app|filesystem|mnt|data)(?:/[\w@.+~\-]+)+)/\b", instruction):
        folder = match.group(1) + "/"
        if any(folder in p for p in found) or folder in ("/app/", "/data/"):
            continue
        found.setdefault(
            folder,
            {
                "path": folder,
                "format": "folder of deliverables",
                "kind": "dir",
                "source": "folder named in the task text",
            },
        )
    return list(found.values())


async def _validate_file(session, path: str, fmt: str) -> str | None:
    """Returns a problem string, or None when the file looks valid."""
    code, out = await sh(
        session,
        f"test -e {shlex.quote(path)} && echo EXISTS && stat -c %s {shlex.quote(path)}",
        timeout=60,
    )
    if code != 0 or "EXISTS" not in out:
        return f"missing: {path}"
    try:
        size = int(out.strip().splitlines()[-1])
    except (IndexError, ValueError):
        size = 0
    if size == 0:
        return f"empty file: {path}"
    ext = path.rsplit(".", 1)[-1].lower()
    if ext in ("zip", "xlsx", "docx", "pptx"):
        checker = (
            "import sys,zipfile;z=zipfile.ZipFile(sys.argv[1]);"
            "bad=z.testzip();"
            "print('BAD' if bad else 'OK');"
            "print('CT' if '[Content_Types].xml' in z.namelist() else 'NOCT')"
        )
        code, out = await sh(
            session, f"python3 -c {shlex.quote(checker)} {shlex.quote(path)}", timeout=120
        )
        if code != 0:
            return f"not a readable zip container: {path}"
        if "BAD" in out:
            return f"corrupt archive entry in {path}"
        if ext != "zip" and "NOCT" in out:
            return f"{path} is a zip but not a valid {fmt}"
    elif ext == "ifc":
        code, out = await sh(session, f"head -c 200 {shlex.quote(path)}", timeout=60)
        if "ISO-10303-21" not in out:
            return f"{path} does not start with an ISO-10303-21 header (not a STEP/IFC file)"
    elif ext == "pdf":
        code, out = await sh(session, f"head -c 5 {shlex.quote(path)}", timeout=60)
        if "%PDF" not in out:
            return f"{path} is not a PDF"
    elif ext == "json":
        code, out = await sh(
            session,
            f"python3 -c 'import json,sys;json.load(open(sys.argv[1]))' {shlex.quote(path)}",
            timeout=60,
        )
        if code != 0:
            return f"{path} is not valid JSON"
    return None


async def check(session, contract: list[dict], final_message: str) -> str:
    """Runs every deterministic check and returns a report for the model."""
    lines: list[str] = []
    problems = 0
    for item in contract:
        path, kind = item["path"], item["kind"]
        if kind == "dir":
            code, out = await sh(
                session, f"find {shlex.quote(path)} -type f 2>/dev/null | head -40", timeout=90
            )
            count = len([x for x in out.splitlines() if x.strip()]) if code == 0 else 0
            if count == 0:
                lines.append(f"FAIL {path}: no files under this graded folder")
                problems += 1
            else:
                lines.append(f"OK   {path}: {count}+ file(s)")
            continue
        problem = await _validate_file(session, path, item["format"])
        if problem:
            lines.append(f"FAIL {problem}")
            problems += 1
        else:
            lines.append(f"OK   {path}")

    # Placeholder sweep over text-shaped deliverables next to the named paths.
    scan_roots = {os.path.dirname(i["path"]) for i in contract if i["kind"] == "file"}
    for root in list(scan_roots)[:6]:
        if not root:
            continue
        needle = "|".join(PLACEHOLDERS)
        code, out = await sh(
            session,
            f"grep -rIl -E {shlex.quote(needle)} {shlex.quote(root)} 2>/dev/null | head -10",
            timeout=120,
        )
        for hit in (out.splitlines() if code == 0 else []):
            if hit.strip():
                lines.append(f"WARN placeholder text left in {hit.strip()}")
                problems += 1

    if len(final_message.strip()) < 40:
        lines.append("FAIL final message is too short to tell the grader what was delivered")
        problems += 1
    else:
        lines.append(f"OK   final message: {len(final_message)} characters")

    head = "CONTRACT CHECK PASSED" if problems == 0 else f"CONTRACT CHECK: {problems} problem(s)"
    return head + "\n" + "\n".join(lines)


async def deliverable_listing(session, contract: list[dict]) -> str:
    """Sizes of the graded files, for the final message."""
    rows = []
    for item in contract:
        path = item["path"]
        if item["kind"] == "dir":
            code, out = await sh(
                session,
                f"find {shlex.quote(path)} -type f -printf '%s %p\\n' 2>/dev/null | head -20",
                timeout=60,
            )
            rows.append(out.strip() or f"(empty) {path}")
        else:
            code, out = await sh(
                session,
                f"stat -c '%s bytes  %n' {shlex.quote(path)} 2>/dev/null",
                timeout=60,
            )
            rows.append(out.strip() or f"(missing) {path}")
    return "\n".join(r for r in rows if r)


def jsonable(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)[:4000]
    except TypeError:
        return str(value)[:4000]
