"""Finish development acceptance in one run: container regression, then the paid Messages case.

The paid step runs only after the regression passes, is skipped once recorded as
passed, and is never retried automatically. Results are read from the journal
because official_acceptance.py exits 0 when a case fails.
"""

import asyncio
import json
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import asyncpg
import httpx

JOURNAL = Path("test-results/official-acceptance.json")
JUNIT = Path("test-results/container-junit.xml")
SMOKE = Path("test-results/container-smoke.json")
OFFICIAL_DATABASE = "postgresql://gateway:gateway@localhost:55432/gateway_official_test"
PAID_RUN = [
    *("uv", "run", "--frozen", "python", "scripts/official_acceptance.py"),
    *("--authorized-budget-cny", "5", "--vendors", "kimi", "--kimi-model", "kimi-k2.6"),
    *("--cases", "tool_stream", "--min-interval-s", "5"),
]


def step(title):
    print(f"\n== {title}", flush=True)


def run(*command):
    return subprocess.run(command, check=False).returncode == 0


def docker_ready():
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


def ensure_docker(timeout_s=180):
    if docker_ready():
        return True
    if shutil.which("orbctl"):
        subprocess.Popen(["orbctl", "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if docker_ready():
            return True
        time.sleep(3)
    return False


def journal_rows():
    return json.loads(JOURNAL.read_text())["requests"] if JOURNAL.exists() else []


def messages_rows(rows):
    return [
        r
        for r in rows
        if r.get("protocol") == "anthropic_messages"
        and r.get("model") == "kimi-k2.6"
        and r.get("case") == "tool_stream_result"
    ]


def messages_passed(rows):
    return any(r["status"] == "passed" for r in messages_rows(rows))


async def unjournaled_kimi_calls():
    """Ledger rows the journal never recorded, such as the call interrupted by the pause."""
    try:
        connection = await asyncpg.connect(OFFICIAL_DATABASE)
    except asyncpg.InvalidCatalogNameError:
        return None
    try:
        rows = await connection.fetch(
            "SELECT id, status, created_at, payload->'cost' AS cost FROM llm_calls "
            "WHERE logical_model='test_kimi' ORDER BY created_at"
        )
    except asyncpg.UndefinedTableError:
        return None
    finally:
        await connection.close()
    known = {r.get("call_id") for r in journal_rows()}
    return [dict(r) for r in rows if r["id"] not in known]


def regression_summary():
    suite = ElementTree.parse(JUNIT).getroot()
    suite = suite if suite.tag == "testsuite" else suite[0]
    return {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}


def gateway_ready(timeout_s=90):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get("http://127.0.0.1:8000/health/ready", timeout=3).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return False


def main():
    step("Docker")
    if not ensure_docker():
        print("Docker is unavailable. Start OrbStack, then rerun.")
        return 1
    if not run("docker", "compose", "up", "-d", "--wait", "postgres", "redis"):
        return 1

    step("Interrupted Messages call")
    orphans = asyncio.run(unjournaled_kimi_calls())
    print("no official test database" if orphans is None else orphans or "no unjournaled Kimi calls")

    step("Container regression")
    tests_ok = run("make", "test")
    summary = regression_summary() if JUNIT.exists() else None
    print(summary)
    if not tests_ok:
        print("Regression failed; the paid case was not run.")
        return 1

    step("Container smoke")
    smoke_ok = (
        run("make", "dev")
        and gateway_ready()
        and run("uv", "run", "--frozen", "python", "scripts/container_smoke.py")
    )
    if not smoke_ok:
        print("Smoke failed; the paid case was not run.")
        return 1

    step("Paid Messages case")
    if messages_passed(journal_rows()):
        print("Already passed; no paid call made.")
    elif not run(*PAID_RUN):
        print("Acceptance script crashed; inspect the journal before any rerun.")
        return 1

    rows = journal_rows()
    print(json.dumps(messages_rows(rows), ensure_ascii=False, indent=2))
    if not messages_passed(rows):
        print("Messages case did not pass. Not retried automatically.")
        return 1
    print("\nAcceptance finished: regression", summary, "and Messages tool_stream_result passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
