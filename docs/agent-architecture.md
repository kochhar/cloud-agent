# Local Cloud Agent — Architecture

## What this is

A local version of a cloud coding agent.

- A user submits a task against a repository.
- A sandboxed container clones the repo,
- an LLM decides what to do,
- tools run inside the sandbox,
- the output is a branch and a diff.

Key non-functional requirements

- the sandbox can die mid-task,
- the control plane can restart,
- the task still has to finish

## Components

```
  ┌────────┐   HTTP    ┌───────────────────┐        ┌────────────┐
  │ client │ ────────▶ │   control plane   │ ─────▶ │  Postgres  │
  └────────┘           │   (stateless, N   │        └────────────┘
                       │    instances)     │
                       └───────────────────┘
                          ▲            │ docker run
                          │ long-poll  ▼
                       ┌───────────────────┐        ┌────────────┐
                       │ sandbox container │ ─────▶ │ git remote │
                       │     (cursord)     │  push  │  (origin)  │
                       └───────────────────┘        └────────────┘
```

**Client.** Creates a session, sends messages, polls for events. No logic beyond rendering.

**Control plane.** An HTTP service. Owns the conversation, calls Grok, decides which tool runs next, spawns and reaps sandboxes. Keeps nothing in memory between requests, so any instance can serve any request for any session.

**cursord.** A daemon inside the sandbox. Asks for work, runs the tool, reports the result. It has no knowledge of the LLM or the conversation, and treats every tool call as an isolated unit of work.

**Postgres.** Source of truth for conversation and session state.

**Operations dashboard.** A standalone read-only service over Postgres. It
serves fleet aggregates independently of any control-plane instance; the
per-session event viewer remains the client served by control. See
[observability.md](observability.md).

**Bare git repo on the host.** Stands in for GitHub. Source of truth for file state.

## State management

A container can disappear at any time, so the design needs to address what survives that and where it lives.

1. Conversation state goes to Postgres, appended as it happens.
    1. Every user message, model response, tool call, and tool result is a row, written before the next step is taken.
2. File state goes to git.
    1. After a tool call, cursord runs `git status --porcelain`.
    2. If the tree is dirty it commits and pushes to the remote, and the resulting SHA is returned to control against that tool call.
    3. Read-only calls produce no commits, which is most of them.
3. Everything else in the container is treated as rebuildable:
    1. installed packages, build output, background processes, shell environment.
    2. Recovery re-does that work rather than preserving it.
    3. Recovery is a rebuild.
        1. Spawn a fresh container,
        2. clone the branch,
        3. resume from the message log using the SHA hash of the latest commit.

### What doesn't work

**A tool call can execute twice.**

1. Recovery re-dispatches pending calls, so if the sandbox died after the command ran but before the result was reported, the command runs again.
2. File writes are idempotent because they write full contents.
3. Arbitrary shell commands are not idempotent. At-least-once execution has a hole.
4. Repeated calls are marked in the transcript so the model can see what happened.

**A dead sandbox may not be dead.**

1. A container that misses heartbeats because it is slow or partitioned can still push commits and report results after a replacement has started.
2. This is handled with an epoch number, described below.

**Shell state does not persist across calls.**

1. Each `run_command` executes independently from the repo root.
2. A `cd` or an exported variable in one call is not visible in the next.
3. This is a deliberate constraint, declared in the tool description so the model works with it.

**Ignored files are invisible.** A file the agent writes that matches `.gitignore` is not committed and does not survive recovery. `.env` is the realistic case. Mitigation for this build: cursord force-adds a small allowlist of paths. The general problem is left unsolved here and should be named as a limitation.

**The model must not touch git.** If git commands are exposed as tools, model-authored commits and checkouts collide with the checkpointing. Git stays entirely on the cursord side and stays out of the tool schema.

**Commit history is noisy.** One commit per change produces a messy log. This does not matter because the deliverable is `git diff base..head`.

## The loop

The agent loop is a state machine advanced by HTTP requests. No process holds it open.

1. cursord long-polls for the next action.
2. The control plane returns a pending tool call, or nothing if the hold expires.
3. cursord executes it, commits and pushes if the tree changed, and posts the result.
4. The handler for that post advances the loop: append the result, call Grok, write the next pending tool call, return.

No background workers, no leases, no in-memory session objects, no affinity between a sandbox and a control-plane instance.

### The loop, as code

The conventional inner loop splits across two HTTP handlers. Everything after the tool call belongs to a different request from everything before it.

```python
# POST /sessions/{id}/messages
def on_user_message(sid, text):
    append_message(sid, role="user", content=text)
    advance(sid)

# POST /sandbox/{sid}/actions/{aid}/result
def on_tool_result(sid, aid, result):
    complete_tool_call(aid, result)
    if pending_tool_calls(sid):
        return                       # parallel calls: wait for the last one
    append_message(sid, role="tool", content=all_results(sid))
    advance(sid)

def advance(sid):
    state = load_messages(sid)       # the context array, as a SELECT
    text, thinking, tools = LLM(state)
    append_message(sid, role="assistant", content=text, tool_calls=tools)
    emit(sid, "text", text)
    emit(sid, "thinking", thinking)
    if not tools:
        set_status(sid, "idle")
        emit(sid, "status", "idle")
    else:
        insert_pending_tool_calls(sid, tools)
        for t in tools:
            emit(sid, "tool_started", t.name, t.args)
```

`advance` is one iteration of the inner loop. It runs when a request arrives that gives it something to do.

### Epochs

Each session has an integer epoch, incremented every time a sandbox is spawned for it. cursord receives its epoch at registration and includes it on every request. The control plane rejects any request whose epoch is not exactly `current_epoch` — older and future values both fail.

A zombie sandbox can therefore push commits to the branch, but its results are never accepted and it is never given more work. The branch is reconciled by resetting to the last SHA the control plane accepted.

## Reporting progress to the client

The client has to see what the agent is doing as it happens. With no process holding the loop and no open connection, telling the client something means writing a row. The client long-polls `GET /sessions/{id}/events?after={seq}` and renders what comes back.

### Two tables

`messages` is the model's context, rebuilt on every LLM call. A user message that lands mid-batch is held in that array until the tool replies close, so the provider never sees a user row splitting an assistant/tool pair. The log itself stays in arrival order.
`events` is the UI feed.

#### Differences

- Tool results go to the model in full and to the client truncated.
- Thinking may or may not be resent to the model, depending on the API.
- Sandbox lifecycle events go to the client only.

### Event types

`thinking`, `text`, `status`, `tool_started`, `tool_finished`, `tool_requeued`, and the sandbox lifecycle set: `sandbox_spawning`, `sandbox_ready`, `sandbox_died`, `sandbox_exited`.

There is no `sandbox_replaced` event. A replacement is already two events: `sandbox_died`, whose payload carries `replaced_by`, and `sandbox_spawning` for the epoch that took over.

`sandbox_exited` is the ordinary end of a container's life and is kept apart from `sandbox_died` on purpose. A feed that reported a death every time a turn ended would train the reader to ignore the word.

We should be able to kill a container mid-task and watch the client report the death, the replacement, and then the completed work shows how this architecture works.

### Sequence numbers

`events.seq` is a per-session counter, incremented inside the same transaction that advances the session. Loop advancement for a single session is already serialized, so a per-session counter has no contention.

### No token streaming

Text and thinking are written as whole events when the LLM call returns.

## Lifecycle

**Start.** The client creates a session and the row exists before any container does. The control plane spawns a container with the session ID and epoch in its environment. cursord registers, clones, and checks out a fresh branch.

**Run.** Tools available to the model: read file, write file, list files, run command. The write path ends in a commit and a push when the tree is dirty.

**Finish.** The model returns a response with no tool calls. The session is marked `idle`. The diff the client reads is whatever the sandbox last reported onto the session row (`last_accepted_sha`, `diff_preview`, `diff_stat`); the full patch stays in the repository.

`idle` is a resting state, not a closed one. The turn is over and the next move belongs to the user, which is a wait with no upper bound, so the sandbox does not sit through it: cursord sees `idle` on its poll, waits out a threshold of its own — five minutes in this build — and exits, reporting that it is going so the control plane can retire the row rather than wait for the heartbeat to go stale.

The threshold lives in the container because the container is what it spends. The control plane would be deciding how long someone else's process should live, and it is the wrong side to be holding that opinion.

**Resume.** A user message on an `idle` session picks the same session up again. If the sandbox has already exited, a new one is spawned at a new epoch; it clones, resets to `last_accepted_sha`, and the pending tool call is dispatched to it. This is the recovery path, run deliberately rather than in response to a death — which is why the design can afford to let an idle container go in the first place. The cost is a clone on the next message, and the thing bought is that an abandoned tab stops costing a machine.

Only `failed` and `cancelled` close a session for good.

**Sandbox dies.** cursord heartbeats every ten seconds. When heartbeats stop for longer than the threshold — thirty seconds, three missed beats — the sandbox row is reaped and the client sees `sandbox_died`. The session stays `thinking` or `executing`. A replacement is spawned at a new epoch; in-flight tool calls are requeued (`tool_requeued`) and dispatched to it.

The two numbers are one decision written in two places, and the threshold has to stay well above the interval. A threshold below three missed beats replaces sandboxes that are merely slow, and each wrong replacement re-runs the tool call the original was in the middle of. It clones, checks out the branch, and starts polling. Any pending tool call is still pending and gets dispatched to the new sandbox.

**Control plane restarts.** Nothing to recover. cursord's poll fails, it retries, another instance answers.

**Repeated crashes.** Each tool call carries an attempt counter. Past a ceiling the session fails rather than spawning containers indefinitely.

## API

### Client-facing

| Method | Path | Purpose |
| :- | :- | :- |
| POST | /sessions | Create a session from a repo URL and initial prompt. Returns session ID. |
| GET | /sessions/{id} | Session status, branch name, current state. |
| POST | /sessions/{id}/messages | Add a user message to an existing session. Advances the loop. |
| GET | /sessions/{id}/events?after={seq} | Long-poll for transcript events after a sequence number. Drives the UI. |
| GET | /sessions/{id}/diff | Diff of the branch against its base. |
| POST | /sessions/{id}/cancel | Stop the session and tear down its sandbox. |

### cursord-facing

| Method | Path | Purpose |
| :- | :- | :- |
| POST | /sandbox/{session_id}/register | Sandbox announces itself with its epoch. Returns repo URL, branch, and base SHA. |
| GET | /sandbox/{session_id}/next-action | Long-poll for the pending tool call. Carries epoch. |
| POST | /sandbox/{session_id}/actions/{action_id}/result | Report a tool result and commit SHA. This call advances the loop. |
| POST | /sandbox/{session_id}/heartbeat | Liveness. Rejected if the epoch is stale, which tells a zombie to shut down. |

### Operational

| Method | Path | Purpose |
| :- | :- | :- |
| POST | /internal/reap | Scan for sessions with expired heartbeats and respawn their sandboxes. Called on a timer. |
| GET | /healthz | Liveness. |

## Schema

The live copy is `control/schema.sql`. Field names below match the columns.

1. Sessions
    1. `id` – uuid, primary key
    2. `repo_url`, text
    3. `branch`, text
    4. `base_sha`, text — where the sandbox's clone started
    5. `last_accepted_sha`, text — newest SHA from a live epoch
    6. `diff_preview`, text — bounded first slice of `base..head`
    7. `diff_stat`, jsonb — per-file counts and totals
    8. `status`, enum
        1. `idle` — the turn ended; no sandbox, and the next move is the user's
        2. `thinking` — an instance is inside the LLM call
        3. `executing` — tool calls outstanding
        4. `failed`
        5. `cancelled`
    9. `current_epoch`, int
    10. `message_seq`, int
    11. `event_seq`, int
    12. `thinking_since`, timestamp — set on entry to `thinking`
    13. `error`, text
2. Sandboxes
    1. `id`, uuid, primary key
    2. `session_id`, uuid, foreign key
    3. `epoch`, int
    4. `container_id`, text — docker id, null until spawned
    5. `status`, text — `spawning`, `ready`, `dead`, `replaced`, `exited`
    6. `last_heartbeat_at`, timestamp
3. Messages
    1. `id`, uuid, primary key
    2. `session_id`, uuid, foreign key
    3. `seq`, int — from `sessions.message_seq`
    4. `role`, text — `system`, `user`, `assistant`, `tool`
    5. `content`, text — null on a pure tool-call turn
    6. `reasoning`, text — thinking, if the API returns it
    7. `tool_calls`, jsonb — assistant rows, the provider array verbatim
    8. `provider_call_id`, text — tool rows, which provider call this answers
4. Tool calls
    1. `id`, uuid, primary key — the `action_id` in the API
    2. `session_id`, uuid, foreign key
    3. `message_id`, uuid, foreign key
    4. `provider_call_id`, text
    5. `name`, text
    6. `args`, jsonb
    7. `ordinal`, int — dispatch order within the batch
    8. `status`, enum — `pending`, `dispatched`, `done`, `failed`
    9. `epoch`, int — which sandbox epoch this call went to
    10. `attempts`, int — dispatches before giving up
    11. `result`, text
    12. `exit_code`, int
    13. `commit_sha`, text — null when the tree was clean
    14. `repeated`, boolean — re-dispatched after a death
    15. `dispatched_at`, timestamp
    16. `completed_at`, timestamp
5. Events
    1. `session_id`, uuid, foreign key
    2. `seq`, int — from `sessions.event_seq`; primary key is `(session_id, seq)`
    3. `type`, text — `thinking`, `text`, `status`, `tool_started`, `tool_finished`, `tool_requeued`, `sandbox_spawning`, `sandbox_ready`, `sandbox_died`, `sandbox_exited`
    4. `payload`, jsonb
