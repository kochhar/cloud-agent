# The sandbox container — outline

Companion to [local-cloud-agent-architecture.md](local-cloud-agent-architecture.md),
which describes cursord in one paragraph. This is that paragraph expanded to
the point where it can be built. Code sketch lives in `sandbox/`.

## What is in the container

One process, `python -m cursord`, and a clone of the repo at `/workspace/repo`.

No LLM credentials, no database connection, no knowledge of the conversation.
cursord receives a tool name and an arguments object and reports what
happened. Everything that decides *which* tool runs stays on the control
plane, which is what allows the container to be discarded without losing a
decision.

```
  ┌─────────────────────── sandbox container ────────────────────────┐
  │                                                                  │
  │   heartbeat task ──── POST /heartbeat ──────┐                    │
  │                                             │                    │
  │   work task                                 ▼                    │
  │     ├── GET  /next-action  (long poll) ─── control plane         │
  │     ├── execute tool ── /workspace/repo                          │
  │     ├── commit + push ─────────────────────────▶ bare git repo   │
  │     └── POST /actions/{id}/result ───────── control plane        │
  │                                                                  │
  └──────────────────────────────────────────────────────────────────┘
```

## Lifecycle

**Spawn.** `control/sandbox.py` bumps the epoch, writes the `sandboxes` row,
then runs the container with `SESSION_ID`, `EPOCH`, `CONTROL_URL`, `REPO_URL`
and `BRANCH` in its environment. Those five values are everything the
container is told directly.

**Register.** cursord posts its epoch and gets back the repo URL, the branch,
and the SHA to resume from. Retried on connection failure, because the
container regularly wins the race against the instance that spawned it.

**Heartbeat starts before the clone.** A large repo can take longer than the
ten-second death threshold. A sandbox reaped while still cloning would never
execute anything, and would be replaced by another sandbox that also gets
reaped while cloning.

**Clone.** Identical on a first spawn and on a rebuild — same code path, a
different starting SHA. If the branch exists on the remote we check it out; if
not we create it. Then `reset --hard` to the resume SHA.

**Run.** Long-poll, execute, checkpoint, report. Repeat.

**Exit.** A 409 on any request means a newer sandbox owns the session. cursord
stops where it is and exits 0. It does not try to finish the call it is
holding, because nothing it produces will be accepted.

## The two rules that make recovery work

**Push before report.** A result is only reported once the file state that
produced it is durable in the bare repo. The control plane can therefore treat
an accepted result and its commit SHA as a single fact.

The cost is a window: a container that pushes and then dies before reporting
leaves a commit nobody accepted. That commit is not lost data, it is
*unattributed* data — no tool result in the log corresponds to it. The
replacement resets past it and re-runs the tool.

**Resume from the last accepted SHA, not the branch tip.** The tip may carry
an unattributed commit from the window above, or a commit pushed by a zombie
that the control plane rejected. `last_accepted_sha` is the authority; the
branch is reconciled to it with a force-push on the next checkpoint.

This is why `git` is not in the tool schema. A model that can `checkout` or
`commit` can invalidate both rules in a single call.

## Concurrency

Two asyncio tasks, and it has to be two.

The work task spends most of its life blocked — 25 seconds inside a long poll,
or arbitrarily long inside `run_command`. If heartbeats were sent from the same
task, every tool call longer than the death threshold would be read as a death,
and the session would spawn a replacement to redo work that was about to
finish. The failure would get worse the more useful the tool call was.

## Tool surface

| Tool | Shape | Limit |
| :- | :- | :- |
| `read_file` | `path` | 256 KiB, truncated |
| `write_file` | `path`, `content` | full contents only, so a re-run is a no-op |
| `list_files` | `path`, `recursive` | 1000 entries |
| `run_command` | `command`, `timeout` | 120s default, own process group, 64 KiB of merged output |

`run_command` starts a new session per call and is killed by process group on
timeout, so a backgrounded child cannot hold the pipe open or outlive the call.
Shell state does not carry between calls — a `cd` or an export is gone by the
next one. That is a constraint, not an oversight: it is what makes a tool call
independent of the container that ran the one before it, and therefore what
makes recovery a rebuild rather than a replay.

No tool raises. A tool that fails returns its failure as output with a nonzero
exit code, because a sandbox that died to report a bad path would cost a whole
epoch to say so.

## Running it

```bash
docker build -t cloud-agent-sandbox:dev sandbox/
export SANDBOX_IMAGE=cloud-agent-sandbox:dev
```

The container can also be run by hand against a live epoch, which is the
fastest way to work on it — `control/sandbox.py` already records the sandbox
row and opens the epoch even when it starts nothing:

```bash
docker run --rm \
  -e SESSION_ID=... -e EPOCH=1 \
  -e CONTROL_URL=http://host.docker.internal:8000 \
  -e REPO_URL=/srv/repos/demo.git -e BRANCH=agent/abc12345 \
  -v /srv/repos/demo.git:/srv/repos/demo.git \
  cloud-agent-sandbox:dev
```

Killing that container mid-task is the demo the architecture doc asks for: the
client should show the death, the replacement, and then the completed work.

## Open questions this sketch ran into

These are contract gaps, not implementation details. Each one needs a decision
on the control-plane side before the container is finished.

1. **A finished session never stops its container.** `next-action` returns a
   null tool both for "nothing pending yet" and for "this session is over", so
   a completed session leaves a container long-polling forever. The sketch
   assumes the response also carries `session_status` and exits on a terminal
   one. The alternative is for `cancel` and completion to `docker kill`, which
   works but leaves the container's exit depending on the control plane being
   up at the right moment.

2. **Who resolves `base_sha`?** The schema comments say "set at first
   register", but register is documented as *returning* the base SHA, and on a
   first spawn the container is the only party holding a clone. Cleaner for the
   spawner to resolve it with `git ls-remote` against the bare repo before the
   container exists, so it is known even if no sandbox ever starts.

3. **`resume_sha` is not in the documented register response.** The doc lists
   repo URL, branch, and base SHA. A rebuild needs `last_accepted_sha` as well,
   and it is not the same value after the first tool call.

4. **The final diff should be `base..last_accepted_sha`, not `base..branch`.**
   The tip can carry unattributed or zombie commits. `GET /sessions/{id}/diff`
   reading the branch would occasionally show work the transcript never
   mentions.

5. **Force-push has to be allowed on the bare repo**, since reconciliation is a
   rewind. Worth asserting at setup rather than discovering during a recovery.

6. **Network egress is unresolved.** The sandbox must reach the control plane,
   and the agent will want to `pip install`. Both argue for an open network,
   which means arbitrary model-authored commands get internet access. Naming it
   as a limitation is probably right for this build.

7. **Linux hosts need `--add-host=host.docker.internal:host-gateway`**, which
   the current spawner does not pass. Docker Desktop resolves it for free; a
   Linux CI box does not.

8. **Teardown needs a handle on the container.** `app.py` calls
   `sandbox.teardown`, which does not exist yet. A `--name` or a label per
   session and epoch makes it a one-liner and also makes an orphan easy to find.
