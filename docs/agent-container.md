# The sandbox container — outline

Companion to [local-cloud-agent-architecture.md](local-cloud-agent-architecture.md),
which describes cursord in one paragraph. This is that paragraph expanded to
the point where it can be built. Code sketch lives in `agent/`.

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
  │     ├── commit + push ─────────────────────────▶ git remote      │
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
death threshold. A sandbox reaped while still cloning would never execute
anything, and would be replaced by another sandbox that also gets reaped
while cloning.

**Clone.** Identical on a first spawn and on a rebuild — same code path, a
different starting SHA. If the branch exists on the remote we check it out; if
not we create it. Then `reset --hard` to the resume SHA.

**Run.** Long-poll, execute, checkpoint, report. Repeat.

**Exit.** A 409 on any request means a newer sandbox owns the session. cursord
stops where it is and exits 0. It does not try to finish the call it is
holding, because nothing it produces will be accepted.

## The two rules that make recovery work

**Push before report.** A result is only reported once the file state that
produced it is durable on the remote. The control plane can therefore treat
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

Names and argument spellings come from `control/tools.py`, which is what the
model is told. cursord only executes; `scripts/check_tools.py` is what keeps
the two in agreement, since they cannot import each other.

| Tool | Arguments | Limit |
| :- | :- | :- |
| `read_file` | `path` | 256 KiB, truncation stated in the output |
| `write_file` | `path`, `contents` | full contents only, so a re-run is a no-op |
| `list_files` | `path`, `recursive` | 1000 entries |
| `run_command` | `command`, `timeout_seconds` | 120s default, own process group, 64 KiB of merged output |

`run_command` starts a new session per call and is killed by process group on
timeout, so a backgrounded child cannot hold the pipe open or outlive the call.
Shell state does not carry between calls — a `cd` or an export is gone by the
next one. That is a constraint, not an oversight: it is what makes a tool call
independent of the container that ran the one before it, and therefore what
makes recovery a rebuild rather than a replay.

No tool raises. A tool that fails returns its failure as output with a nonzero
exit code, because a sandbox that died to report a bad path would cost a whole
epoch to say so.

## Git credentials

Every repo is a real remote. The container needs a key for the clone and the
same key again for every checkpoint push, and how that key gets in is a
security decision rather than a plumbing one.

The container executes model-authored shell commands. Anything readable in its
filesystem is readable by the agent, so a bind-mounted private key is a key the
agent can print into a tool result — where it would then be written to the
`tool_calls` table and replayed into the model's context on every subsequent
turn. `SANDBOX_SSH_MODE` picks between:

| Mode | What the container gets | Cost |
| :- | :- | :- |
| `agent` (default) | the host ssh-agent socket, forwarded | key never enters the container; the agent can still *use* it while running |
| `keys` | `~/.ssh` bind-mounted read-only | private key readable by any model-authored command |

Agent forwarding is the default because it removes the exfiltration path
without removing the capability. It does not remove the capability itself:
while the container lives, anything running in it can push wherever that key
can push. A deploy key scoped to one repository is the way to bound that, and
is worth doing before pointing this at anything that matters.

Either way `ssh-add` has to have been run on the host — the socket only offers
keys the agent is actually holding.

`known_hosts` is mounted in both credentialed modes. The image sets
`StrictHostKeyChecking=yes`, so without it the clone refuses the connection;
the alternative of disabling the check would have the container accept any host
key for the remote it pushes to. `BatchMode=yes` is set for a duller reason: an
unattended container that hits an interactive prompt hangs until the spawn
timeout instead of failing with a reason.

```bash
ssh-keyscan github.com >> ~/.ssh/known_hosts   # once
ssh-add ~/.ssh/id_ed25519                      # per login
```

## Running it

```bash
docker build -t cloud-agent-sandbox:dev agent/
export SANDBOX_IMAGE=cloud-agent-sandbox:dev
```

The container can also be run by hand against a live epoch, which is the
fastest way to work on it — `control/sandbox.py` already records the sandbox
row and opens the epoch even when it starts nothing.

The host's agent is forwarded for the clone and the pushes. The socket path
below is Docker Desktop's; on Linux bind `$SSH_AUTH_SOCK` instead.

```bash
docker run --rm \
  -e SESSION_ID=... -e EPOCH=1 \
  -e CONTROL_URL=http://host.docker.internal:8000 \
  -e REPO_URL=git@github.com:acme/widgets.git -e BRANCH=agent/abc12345 \
  -v ~/.ssh/known_hosts:/root/.ssh/known_hosts:ro \
  -v /run/host-services/ssh-auth.sock:/ssh-agent -e SSH_AUTH_SOCK=/ssh-agent \
  cloud-agent-sandbox:dev
```

Killing that container mid-task is the demo the architecture doc asks for: the
client should show the death, the replacement, and then the completed work.

## Open questions this sketch ran into

These are contract gaps, not implementation details. Each one needs a decision
on the control-plane side before the container is finished.

1. ~~**A finished session never stops its container.**~~ *Settled.*
   `next-action` carries `session_status` alongside the tool, so a null tool
   is no longer ambiguous. `failed` and `cancelled` stop the container at
   once. `idle` does not, because the session can still be resumed by a user
   message: cursord starts a clock on the first idle answer, resets it on any
   work, and leaves once the session has been idle for `IDLE_EXIT_SECONDS`,
   five minutes by default. On the way out it posts a heartbeat with
   `exiting` set, so the sandbox row is retired deliberately rather than by
   going stale. The rejected alternative was `docker kill` from the control
   plane, which makes a container's exit depend on the control plane being up
   at the right moment.

2. **Who resolves `base_sha`?** The schema comments say "set at first
   register", but register is documented as *returning* the base SHA, and on a
   first spawn the container is the only party holding a clone. Cleaner for the
   spawner to resolve it with `git ls-remote` against the remote before the
   container exists, so it is known even if no sandbox ever starts.

3. **`resume_sha` is not in the documented register response.** The doc lists
   repo URL, branch, and base SHA. A rebuild needs `last_accepted_sha` as well,
   and it is not the same value after the first tool call.

4. **The final diff should be `base..last_accepted_sha`, not `base..branch`.**
   The tip can carry unattributed or zombie commits. `GET /sessions/{id}/diff`
   reading the branch would occasionally show work the transcript never
   mentions.

5. **Force-push has to be allowed on the remote**, since reconciliation is a
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

9. **A container that dies before claiming a tool call respawns forever.**
   The architecture doc bounds repeated crashes with the per-tool-call attempt
   counter, but that only counts once a call has been *dispatched*. A sandbox
   that fails during clone never claims one, so attempts stays at zero: it
   dies, misses heartbeats, gets reaped, and is replaced by a container that
   fails the same way. A mounted path could barely fail a clone and this was
   hard to hit; now that every repo is a real remote, a missing key, an
   unknown host, or a revoked deploy key all land exactly here. The session
   needs a spawn ceiling of its own, independent of tool attempts, and there
   is currently no way for cursord to report "I could not start" — it has no
   endpoint for a failure that happens before the first result.
