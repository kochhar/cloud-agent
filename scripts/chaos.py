#!/usr/bin/env python3
"""Chaos harness.

Starts N sessions against the control plane, kills live sandboxes at random
while they work, and reports what survived.

The point is to turn "the architecture recovers from sandbox death" into a
number. Run it once with --no-chaos for a baseline, once with chaos, and
compare completion rates.

    python scripts/chaos.py --sessions 8 --kill-every 20
    python scripts/chaos.py --sessions 8 --no-chaos          # baseline

Reads the sandboxes table directly to find things to kill, rather than
scraping `docker ps`, because the control plane starts containers with no
labels and the process runtime has no containers at all. The DB knows about
both.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

import httpx
import psycopg
from psycopg.rows import dict_row

CONTROL_URL = os.environ.get("CONTROL_URL", "http://127.0.0.1:8000")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/project1"
)


def _ui_root() -> str:
    """Where the browser client lives, for the links printed below.

    Derived from the API url because the two are one deployment: nginx serves
    /api and /ui side by side, and a control plane on its own serves /ui
    itself. Set UI_URL when that stops being true.
    """
    configured = os.environ.get("UI_URL")
    if configured:
        return configured.rstrip("/")
    root = CONTROL_URL[: -len("/api")] if CONTROL_URL.endswith("/api") else CONTROL_URL
    return root.rstrip("/") + "/ui"


UI_ROOT = _ui_root()


def follow_url(session_id: str) -> str:
    """A link that opens this session's event feed in the client."""
    return "{}/?session={}".format(UI_ROOT, session_id)

# Terminal for our purposes. 'idle' is the ordinary end of a turn, and we only
# ever send one prompt per session.
DONE = {"idle", "failed", "cancelled"}


@dataclass
class Run:
    session_id: str
    started: float
    finished: float | None = None
    status: str | None = None
    kills: int = 0
    events: int = 0
    error: str | None = None

    @property
    def seconds(self) -> float | None:
        return None if self.finished is None else self.finished - self.started


runs: dict[str, Run] = {}
pending: list[Run] = []  # sessions created but not yet in runs, for the killer


# ---------------------------------------------------------------------------
# driving one session
# ---------------------------------------------------------------------------
async def drive(client: httpx.AsyncClient, repo_url: str, prompt: str,
                timeout: float) -> Run:
    """Create a session and follow its event feed until it settles."""
    response = await client.post(
        "/sessions", json={"repo_url": repo_url, "prompt": prompt}, timeout=60.0
    )
    response.raise_for_status()
    session_id = response.json()["session_id"]

    run = Run(session_id=session_id, started=time.monotonic())
    runs[session_id] = run
    print(f"  started {session_id[:8]}  {follow_url(session_id)}")

    after = 0
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            # The control plane holds this open, so the loop is cheap.
            feed = await client.get(
                f"/sessions/{session_id}/events",
                params={"after": after, "limit": 200},
                timeout=40.0,
            )
            feed.raise_for_status()
        except httpx.HTTPError as exc:
            # A control plane restart is not a session failure.
            print(f"  {session_id[:8]} feed error: {exc}")
            await asyncio.sleep(1.0)
            continue

        body = feed.json()
        after = body["next_after"]
        run.events += len(body["events"])

        for event in body["events"]:
            if event["type"] == "status":
                status = event["payload"].get("status")
                if status in DONE:
                    run.status = status
                    run.finished = time.monotonic()
                    print(
                        f"  {session_id[:8]} {status} "
                        f"after {run.seconds:.0f}s, {run.kills} kills"
                    )
                    return run

    run.status = "timeout"
    run.finished = time.monotonic()
    run.error = f"no terminal status within {timeout:.0f}s"
    print(f"  {session_id[:8]} TIMED OUT after {run.seconds:.0f}s")
    return run


# ---------------------------------------------------------------------------
# killing things
# ---------------------------------------------------------------------------
def live_sandboxes() -> list[dict]:
    """Sandboxes that are registered and whose session is mid-work.

    Only sessions in 'thinking' or 'executing' — killing a sandbox attached
    to an idle session proves nothing, because it was about to leave anyway.
    """
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        return conn.execute(
            """
            SELECT sb.session_id, sb.epoch, sb.container_id, s.status
              FROM sandboxes sb
              JOIN sessions s ON s.id = sb.session_id
             WHERE sb.status = 'ready'
               AND sb.container_id IS NOT NULL
               AND s.status IN ('thinking', 'executing')
               AND sb.epoch = s.current_epoch
            """
        ).fetchall()


def kill(handle: str) -> bool:
    """Kill a sandbox by the handle the control plane recorded.

    'local:PID' is the process runtime; anything else is a docker id. SIGKILL
    either way: the whole point is an ungraceful death, so the sandbox never
    gets to send its exit beat.
    """
    if handle.startswith("local:"):
        try:
            os.kill(int(handle.split(":", 1)[1]), signal.SIGKILL)
            return True
        except (ProcessLookupError, ValueError):
            return False

    finished = subprocess.run(
        ["docker", "kill", handle], capture_output=True, text=True
    )
    return finished.returncode == 0


async def chaos_loop(interval: float, stop: asyncio.Event) -> int:
    """Kill one live sandbox every `interval` seconds until told to stop."""
    killed = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return killed
        except asyncio.TimeoutError:
            pass

        candidates = await asyncio.to_thread(live_sandboxes)
        if not candidates:
            continue

        victim = random.choice(candidates)
        handle = victim["container_id"]
        session_id = str(victim["session_id"])

        if await asyncio.to_thread(kill, handle):
            killed += 1
            run = runs.get(session_id)
            if run:
                run.kills += 1
            # The link again, on the line that says a sandbox just died: this
            # is the moment there is something worth watching, and scrolling
            # back to where the session started is a poor way to find it.
            print(
                f"  KILLED {handle} — session {session_id[:8]} "
                f"epoch {victim['epoch']} ({victim['status']})\n"
                f"         follow {follow_url(session_id)}"
            )
        else:
            print(f"  kill failed for {handle}")

    return killed


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------
REPORT_SQL = """
SELECT
  (SELECT count(*) FROM tool_calls WHERE session_id = ANY(%(ids)s))
      AS tool_calls,
  (SELECT count(*) FROM tool_calls
     WHERE session_id = ANY(%(ids)s) AND repeated)
      AS repeated_calls,
  (SELECT count(*) FROM tool_calls
     WHERE session_id = ANY(%(ids)s) AND status = 'failed')
      AS failed_calls,
  (SELECT coalesce(max(attempts), 0) FROM tool_calls
     WHERE session_id = ANY(%(ids)s))
      AS max_attempts,
  (SELECT count(*) FROM tool_calls
     WHERE session_id = ANY(%(ids)s) AND status IN ('pending','dispatched'))
      AS stranded_calls,
  (SELECT count(*) FROM sessions
     WHERE id = ANY(%(ids)s) AND status = 'executing')
      AS stuck_executing,
  (SELECT count(*) FROM sessions
     WHERE id = ANY(%(ids)s) AND status = 'thinking')
      AS stuck_thinking,
  (SELECT coalesce(max(current_epoch), 0) FROM sessions
     WHERE id = ANY(%(ids)s))
      AS max_epoch,
  (SELECT count(*) FROM sandboxes
     WHERE session_id = ANY(%(ids)s) AND status = 'dead')
      AS sandboxes_dead
"""


def report(finished: list[Run], killed: int) -> None:
    ids = [run.session_id for run in finished]

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        counts = conn.execute(REPORT_SQL, {"ids": ids}).fetchone()

    completed = [r for r in finished if r.status == "idle"]
    hit = [r for r in finished if r.kills]
    recovered = [r for r in hit if r.status == "idle"]
    durations = sorted(r.seconds for r in finished if r.seconds is not None)

    def line(label: str, value) -> None:
        print(f"  {label:<28} {value}")

    print("\n" + "=" * 58)
    print("  CHAOS REPORT")
    print("=" * 58)

    line("sessions", len(finished))
    line("completed", f"{len(completed)} ({_pct(len(completed), len(finished))})")
    line("failed", sum(1 for r in finished if r.status == "failed"))
    line("timed out", sum(1 for r in finished if r.status == "timeout"))

    print()
    line("sandboxes killed", killed)
    line("sessions hit", len(hit))
    if hit:
        line("recovered", f"{len(recovered)} ({_pct(len(recovered), len(hit))})")
    line("max epoch reached", counts["max_epoch"])
    line("sandboxes marked dead", counts["sandboxes_dead"])

    print()
    line("tool calls", counts["tool_calls"])
    line("re-executed", counts["repeated_calls"])
    line("failed", counts["failed_calls"])
    line("max attempts on one call", counts["max_attempts"])

    print()
    line("stranded tool calls", counts["stranded_calls"])
    line("sessions stuck executing", counts["stuck_executing"])
    line("sessions stuck thinking", counts["stuck_thinking"])

    if durations:
        print()
        line("duration p50", f"{_percentile(durations, 0.5):.0f}s")
        line("duration p95", f"{_percentile(durations, 0.95):.0f}s")

    if counts["stranded_calls"] or counts["stuck_executing"]:
        print(
            "\n  Stranded work means a sandbox died and nothing replaced it.\n"
            "  That is the reaper, or the absence of one."
        )
    print("=" * 58 + "\n")


def _pct(part: int, whole: int) -> str:
    return "0%" if not whole else f"{100 * part / whole:.0f}%"


def _percentile(values: list[float], q: float) -> float:
    return values[min(int(q * len(values)), len(values) - 1)]


# ---------------------------------------------------------------------------
async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-url", default=os.environ.get("CHAOS_REPO_URL", ""),
                        help="repo the sessions work against")
    # Written for the papa-dobles seed: 28 undocumented functions across eight
    # small modules, one checkpoint each. Naming the runner keeps sessions from
    # spending tool calls working out how to test, and forbidding behaviour
    # changes means a red suite afterwards is a recovery bug rather than the
    # model being creative.
    parser.add_argument("--prompt", default="Add a docstring to every function "
                        "in toolkit/ that does not have one, then run "
                        "./run_tests.sh. Do not change any behaviour.")
    parser.add_argument("--sessions", type=int, default=5)
    parser.add_argument("--kill-every", type=float, default=25.0,
                        help="seconds between kills")
    parser.add_argument("--no-chaos", action="store_true",
                        help="run the load without killing anything")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="per-session deadline")
    parser.add_argument("--stagger", type=float, default=2.0,
                        help="seconds between session starts")
    args = parser.parse_args()

    if not args.repo_url:
        parser.error("--repo-url is required (or set CHAOS_REPO_URL)")

    mode = "baseline" if args.no_chaos else f"chaos every {args.kill_every:.0f}s"
    print(f"\n{args.sessions} sessions against {args.repo_url} — {mode}\n")

    stop = asyncio.Event()
    killer = (
        None if args.no_chaos
        else asyncio.create_task(chaos_loop(args.kill_every, stop))
    )

    async with httpx.AsyncClient(base_url=CONTROL_URL) as client:
        async def staggered(index: int):
            await asyncio.sleep(index * args.stagger)
            return await drive(client, args.repo_url, args.prompt, args.timeout)

        results = await asyncio.gather(
            *(staggered(i) for i in range(args.sessions)),
            return_exceptions=True,
        )

    stop.set()
    killed = await killer if killer else 0

    finished = [r for r in results if isinstance(r, Run)]
    for failure in (r for r in results if not isinstance(r, Run)):
        print(f"  harness error: {failure}")

    report(finished, killed)

    # Non-zero if anything was left stranded, so this can gate a commit.
    return 0 if all(r.status == "idle" for r in finished) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
