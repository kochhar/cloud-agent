# The nudger

Recovery in this system is not a special path. Every piece of durable state
already knows how to be resumed: `messages` is the conversation, `tool_calls`
is the work ledger, and `last_accepted_sha` is the authority on file state.
What is missing is the thing that *notices* when a session has stopped moving
and pushes it again.

That is a nudge. Not a repair — a nudge never invents state, never writes a
result, and never decides what the model should do. It finds a session that is
stuck in a state nothing will leave on its own, and applies the one transition
that was supposed to happen.

Everything here lives in `control/nudges.py`.

---

## 1. What gets stuck, and why

Four wedges, each with a different cause and a different fix.

### 1.1 A tool call dispatched to a sandbox that died

`_claim_one_action` selects `status = 'pending'` and nothing else. A row left
at `'dispatched'` when its sandbox died is invisible to every future sandbox,
forever. The session sits in `'executing'` waiting for a result that no
process is going to produce.

### 1.2 A user message nobody answered

`add_user_message` appends the message and then calls `advance`. `advance`
also re-checks `_last_message_role` on the way out, to catch a message that
arrived mid-model-call. Both of those run in a process. If the process dies
between the message committing and the advance, the message is durable and
the intent to answer it is not.

### 1.3 A session wedged in `thinking`

`_claim_thinking` moves `idle` or `executing` into `thinking`. It will not
move `thinking` into anything. So an instance that dies inside the model call
leaves a row that no future `advance` can ever claim. `thinking_since` exists
for exactly this and nothing reads it.

### 1.4 A completed tool batch nobody advanced

This is the subtle one and it gets its own section below.

---

## 2. The nudge model

Three properties, and every nudge must have all three.

**Idempotent.** Two instances running the same scan at the same moment must
not do the work twice. In most cases this falls out of the transition being a
guarded `UPDATE` — the second one matches zero rows. Where it does not, the
scan claims rows with `FOR UPDATE SKIP LOCKED`, which is already the pattern
in `_claim_one_action`.

**Epoch-guarded.** Anything touching a sandbox or a tool call compares
against `sessions.current_epoch`. A nudge must never resurrect work for an
epoch that has been superseded.

**Safe to run at any cadence.** A nudge firing early, on a healthy session,
must be a no-op rather than a duplicate. This is what lets the same code run
on a timer and at startup without a separate "is it safe yet" story.

### Driving them

Every instance runs the same loop, because all three properties above hold.
No leader election, no singleton. All three triggers call `nudges.run_once`,
which runs every nudge and catches per-nudge exceptions so one bad session
cannot stop recovery for the rest of the deployment.

- **At startup**, from the lifespan hook, before the server accepts traffic. A
  restart is the single most likely moment for a session to be stranded,
  because the process that was going to advance it is the one that just died.
  Run in a worker thread, since a pass can spawn containers and blocking the
  event loop would delay the health check that says we are up.
- **Periodically**, every `NUDGE_INTERVAL_SECONDS` (15), from a daemon thread
  started by the same hook and stopped by a `threading.Event` on shutdown. A
  pass that moves nothing logs nothing.
- **On demand**, via `POST /internal/reap`, which runs one pass synchronously
  and returns what it moved. This is how recovery gets triggered and
  inspected by hand instead of only waited for.

Ordering within a pass: sandboxes first, because replacing one rescues its
tool calls on the way through and leaves the second nudge with less to do.

---

## 3. `SandboxNudge`

Owns the sandbox lifecycle. Three scans, all ending in the same action —
spawn a replacement at a new epoch — reached from three different states.

```
SandboxNudge.run()                 -> all three, returns a count per scan
SandboxNudge.reap_expired()        -> 'ready' but the heartbeat stopped
SandboxNudge.reap_announced_dead() -> 'dead', reported by cursord on its way out
SandboxNudge.reap_stuck_spawning() -> 'spawning' and the runtime never came back
```

### 3.1 `reap_expired` — the heartbeat stopped

```sql
SELECT ... FROM sandboxes s JOIN sessions e ON e.id = s.session_id
 WHERE s.status = 'ready'
   AND s.epoch = e.current_epoch
   AND e.status NOT IN ('failed','cancelled')
   AND s.last_heartbeat_at < now() - interval '<threshold>'
   FOR UPDATE OF s SKIP LOCKED
```

Supported by `sandboxes_live_heartbeat_idx`, which is partial on
`status = 'ready'` and exists for this query.

The threshold has to be a multiple of cursord's `HEARTBEAT_INTERVAL` (10s), so
that a single dropped beat is not a death sentence. Three missed beats is the
intent.

### 3.2 `reap_announced_dead` — it told us it crashed

```sql
WHERE s.status = 'dead' AND s.epoch = e.current_epoch
  AND e.status NOT IN ('failed','cancelled')
```

This needs to be a separate predicate, not a wider version of 3.1, and the
reason is worth writing down because it is a consequence of the exit-beat
feature rather than something obvious.

A sandbox that crashes politely sends a final heartbeat with
`reason="crashed"`. `sandbox.heartbeat` maps that through `CLEAN_EXITS` to
`status = 'dead'` — and sets `last_heartbeat_at = now()` while doing it. So
the row is not `'ready'`, which hides it from 3.1, and its heartbeat is
*fresh*, which hides it from any purely time-based scan. Without this arm, a
crash that announces itself recovers more slowly than one that says nothing.

### 3.3 `reap_stuck_spawning` — the runtime never came back

```sql
WHERE s.status = 'spawning' AND s.created_at < now() - interval '<spawn timeout>'
```

`_start_sandbox` returns `None` rather than raising when a runtime cannot
start, which leaves the row at `'spawning'` — not `'ready'`, so 3.1 misses it,
and not `'dead'`, so 3.2 misses it. This closes the first of the two TODOs in
`spawn`. Threshold is `SPAWN_TIMEOUT_SECONDS`, with headroom.

### 3.4 The shared action

All three end in the same two steps:

1. Mark the old row. `'dead'` for 3.1 and 3.3; 3.2 is already `'dead'`. Emit
   `sandbox_died` so the client shows it.
2. Call `sandbox.spawn`, which bumps the epoch, writes a new `sandboxes` row,
   and requeues orphaned tool calls in the same transaction.

**`'exited'` is never matched by any of these.** A sandbox that shut down
cleanly — an idle session, or a session that closed — must not be replaced.
This is what makes `CLEAN_EXITS` load-bearing rather than cosmetic: get it
wrong and an idle session respawns a sandbox that goes idle and leaves,
forever.

### 3.4a The invariant: a session with work has a sandbox

Tool calls are only ever run by a sandbox, so a session without one cannot
execute what a turn produces. That is an invariant, and it belongs at the
points that give a session work rather than inside the loop that consumes it:

```python
sandbox.ensure(session_id)   # spawns only if no live row at the current epoch
advance(session_id)
```

`create_session` already had this shape. There are two other places that need
it, and only two:

| call site | needs `ensure`? |
|---|---|
| `create_session` | already spawns, then advances |
| `add_user_message` | **yes** — an idle session loses its sandbox |
| `advance`'s tail recursion | no, inherits what the caller ensured |
| `record_action_result`'s deferred advance | no, the sandbox just posted a result |
| `nudges._advance` | **yes**, and covers all three `SessionNudge` scans |

Ordered spawn-then-advance, so the container boots while the model thinks.
Keeping it out of `advance` also keeps `advance` free of any dependency on the
runtime, which is what lets it be tested with nothing but a scripted client.

`ensure` passes `expect_epoch` but no `reason`: it is exactly-once against
other callers the same way replacement is, and nothing died — the previous
sandbox left when the session went idle.

### 3.4b The backstop: pending work with no sandbox

```
SandboxNudge.spawn_for_pending()  -> 'executing' with pending calls, no live row
```

With the invariant enforced above this should normally find nothing. It exists
for the one interleaving `ensure` cannot see:

```
t0  cursord's poll returns session_status='idle'; its idle timer has expired
t1  it decides to leave and starts shutting down
t2  a user message arrives. ensure asks, the row still says 'ready',
    so the invariant looks satisfied and nothing spawns
t3  the model asks for tools. Session -> 'executing', calls -> 'pending'
t4  cursord's farewell heartbeat lands. Row -> 'exited', which is final
```

cursord does not re-check between deciding to leave and leaving, so nothing on
that side closes the window either. The session is left `'executing'` behind an
`'exited'` row, which none of 3.1–3.3 match: they want `'ready'`, `'dead'` and
`'spawning'` respectively.

This is the same relationship `ToolCallNudge` has with `spawn`'s inline rescue
(§4.0): enforce inline where the need is visible, and scan for the
interleavings it is not. The scan also differs in where it starts. The others
all begin at a `sandboxes` row; here the problem is the absence of one:

```sql
WHERE e.status = 'executing'
  AND EXISTS (SELECT 1 FROM tool_calls t
               WHERE t.session_id = e.id AND t.status = 'pending')
  AND NOT EXISTS (SELECT 1 FROM sandboxes s
                   WHERE s.session_id = e.id
                     AND s.epoch = e.current_epoch
                     AND s.status IN ('spawning','ready'))
```

It runs last in the pass, because 3.1–3.3 are what turn a row that only
*looks* live into a real replacement. Given `'executing'` with an expired
`'ready'` row, `reap_expired` spawns first and this scan then finds the
invariant already satisfied, so there is no double spawn.

Like `ensure`, it calls `spawn` with **no `reason`**, for the same reason.

### 3.5 A ceiling on replacement

Respawns need a bound of their own. `MAX_TOOL_ATTEMPTS` bounds how many times
one tool call can be retried, but a sandbox that dies during *registration*
never claims a call, so that counter never moves and the loop is unbounded.

**The ceiling counts losses, not epochs.** `current_epoch` looks like the
natural counter and is the wrong one, because 3.4a opens an epoch on every
ordinary resume: a healthy conversation with ten idle gaps would fail with
"gave up after 10 sandboxes" without a single crash. So `spawn` counts
`sandboxes` rows at `'dead'` or `'replaced'` and compares that to
`MAX_SANDBOX_LOSSES`. A resume that ends `'exited'` costs nothing.

Past the ceiling, fail the session with the reason rather than spawning again.

A refinement not made: the count is lifetime, not consecutive, so a very long
session could accumulate unrelated deaths over hours and eventually stop
retrying. Resetting on a successful registration would fix that if it matters.

---

## 4. `ToolCallNudge`

```
ToolCallNudge.run()              -> rescue_orphaned
ToolCallNudge.rescue_orphaned()  -> 'dispatched' at a dead epoch -> 'pending'
```

The transition itself is `sessions.rescue_orphaned_calls(cur, session_id)`,
which lives next to the rest of the `tool_calls` SQL and takes a cursor so it
can join a caller's transaction. Two callers:

- **`spawn`**, in the same transaction as the epoch bump, so a replacement
  finds its work already waiting instead of polling an empty queue.
- **`ToolCallNudge`**, per session, as the backstop below.

Same predicate either way, written once.

Three things it must **not** do:

- **Do not reset `attempts`.** It is what makes `MAX_TOOL_ATTEMPTS` a real
  ceiling and what sets `repeated` on the next dispatch. Resetting it turns a
  crash loop into an infinite one and hides the re-execution from the model.
- **Do not clear `epoch` or `dispatched_at`.** Together they are the record of
  where the lost attempt went and when, and `_claim_one_action` overwrites
  both on re-dispatch.
- **Do not touch rows at the current epoch.** Those are in flight, not stuck.

It emits `tool_requeued` so the feed can show a call changing hands.

### 4.0 Why the backstop is not redundant

If `spawn` always rescues, it is fair to ask what the scan is for. There is one
interleaving `spawn` cannot cover, because the claim commits after it:

```
claim_next_action reads current_epoch and passes its check
spawn bumps the epoch and rescues — finding nothing dispatched yet
claim_next_action commits, marking the call dispatched at the now-old epoch
```

The call is now dispatched to a sandbox that has already been replaced, having
missed the only rescue that was going to look for it. Without the scan it is
stuck permanently and the session never leaves `'executing'`.

### 4.1 Dispatch order

Rescue is also what makes ordering matter, so the two ship together.

`_claim_one_action` used to hand out `ORDER BY created_at LIMIT 1`, and
`created_at` defaults to `now()` — the *transaction* timestamp. Every call in
a batch is inserted in one transaction, so they all tie and the order a
parallel batch runs in was whatever the planner felt like. The model emits a
batch expecting front-to-back execution (a `write_file`, then a command that
reads the file it wrote) and cursord runs one call at a time, so dispatch
order *is* execution order.

Rescue makes it sharper: putting call 1 back to `'pending'` alongside the
untouched calls 2 and 3 means the next claim has to pick 1, and on a tie it
might not.

So `tool_calls` gains `ordinal`, the index within the batch, and the two
queries that order calls use it: `ORDER BY created_at, ordinal` for dispatch
(oldest batch first, then model order within it) and `ORDER BY ordinal` for
assembling tool messages at batch completion.

### 4.1 Why late results are already refused

The plan says results for requeued calls must no longer be accepted. That is
already true, and it is worth being precise about which guard does it, because
there are two and they produce different errors.

`record_action_result` starts with `_require_current_epoch`. A result from the
dead epoch fails there with `StaleEpoch` → 409, and cursord treats 409 as
"you have been replaced" and exits. That is the normal path and it does not
depend on the requeue having happened.

If the epoch had *not* been bumped, the UPDATE's `status = 'dispatched'`
predicate would miss the now-`'pending'` row, `_already_accepted` would be
false, and `_rejected` would return `ActionNotDispatched` → 409.

So the requeue does not need to defend against late results. But it does need
to be **in the same transaction as the epoch bump**, so there is never a
moment where a row is `'pending'` and the old sandbox is still current — which
is the one ordering that would let the dying sandbox claim its own work back.
Putting the requeue inside `spawn` gives this for free.

---

## 5. `SessionNudge`

```
SessionNudge.run()                  -> all three
SessionNudge.wake_unanswered()      -> idle, last message is 'user'
SessionNudge.unwedge_thinking()     -> thinking past the bound
SessionNudge.resume_stalled_batch() -> executing, nothing outstanding
```

All three end in `advance(session_id)`, which is already safe to call
concurrently: `_claim_thinking` is a guarded `UPDATE`, so if another instance
or a live `BackgroundTask` got there first, the second call returns
immediately without touching the model. That single property is what makes
this whole class cheap.

### 5.0 The hand-off, and why it is not inline

`advance` runs the model. It can take minutes, and it recurses into another
turn when a user message is waiting. A pass runs every 15 seconds and the
startup pass runs *before the server accepts traffic*, so calling `advance`
inline would stall recovery behind one slow session and delay the health
check through a whole turn.

So each session is handed to its own daemon thread and the pass moves on:

- **Daemon**, so a shutdown abandons the turn instead of waiting for it.
  Nothing is lost, because an abandoned advance is precisely the state
  `unwedge_thinking` recovers — the next instance picks it up.
- **`NUDGE_MAX_ADVANCES`** caps how many run at once, so a large backlog
  cannot spawn a thread per session. Sessions over the cap wait for a later
  pass.
- **An in-process set of session ids** skips a session already being
  advanced here. Not a correctness guard — `_claim_thinking` is that — but
  without it a scan every 15 seconds over an advance that takes minutes
  re-hands the same session on every pass and grows a thread each time.

### 5.1 `wake_unanswered`

```sql
WHERE e.status = 'idle'
  AND (SELECT role FROM messages WHERE session_id = e.id
        ORDER BY seq DESC LIMIT 1) = 'user'
```

A user spoke and the turn ended without an assistant message after it.
Action: `advance`.

Note this also covers a case `add_user_message` handles in-process: the
session was busy, the nudge to advance was left to `advance` itself, and the
process died before it ran.

### 5.2 `unwedge_thinking`

```sql
WHERE status = 'thinking' AND thinking_since < now() - interval '<bound>'
```

Supported by the existing partial index on `thinking_since`.

Action: put the status back to what the outstanding work implies.

- any `tool_calls` in `('pending','dispatched')` → `'executing'`, **and stop**
- otherwise → `'idle'`, then `advance`

Releasing is the repair on its own, because `'thinking'` is the one status
`_claim_thinking` will not take — a session left there can never be advanced
by anything.

The release is a compare-and-swap: the `UPDATE` repeats the scan's predicate,
so an instance that legitimately claimed the session between the scan and the
write keeps it.

**Only the idle branch is advanced**, which is a correction to the earlier
plan. A session with live tool calls has a sandbox still working on them, and
`record_action_result` will advance it when the batch closes. Advancing it
here would put a second batch in front of a model that is still waiting on
the results of the first, which corrupts the context rather than repairing
it.

For the idle branch the turn is lost, not corrupted: the model call happens
outside any transaction, so a death mid-call left nothing behind. Re-running
costs tokens and may produce different tool calls, and both are fine because
the first attempt wrote nothing.

**The bound is the one number here that cannot be guessed.** It has to exceed
the worst-case *successful* turn, or this nudge will interrupt a live call and
run the model twice on the same context. That worst case is
`GROK_TIMEOUT_SECONDS × GROK_MAX_ATTEMPTS` plus the backoff between attempts —
300 × 4 today, so roughly 20 minutes. Derive it from those two config values
rather than writing a literal, so raising the provider timeout cannot silently
make this nudge aggressive.

### 5.3 `resume_stalled_batch` — item 3

The state to recover, exactly. When the last result of a batch arrives,
`record_action_result`:

1. flips the final `tool_calls` row to `'done'`
2. writes one `messages` row with `role='tool'` per call in the batch
3. leaves `sessions.status` at `'executing'`
4. commits
5. hands `advance` to a FastAPI `BackgroundTask`

Steps 1–4 are one transaction, so the tool messages and the final result are
either both durable or neither. Step 5 is not in it. A process that dies
between the commit and the background task leaves a session whose model
context is complete and whose status says work is still outstanding.

The cursord retry cannot rescue this. A retried result hits
`_already_accepted`, which returns `batch_complete: false` and deliberately
does not advance — correct for a genuine duplicate, and useless here.

**Detection:**

```sql
WHERE e.status = 'executing'
  AND e.updated_at < now() - interval '<grace>'
  AND NOT EXISTS (SELECT 1 FROM tool_calls
                   WHERE session_id = e.id
                     AND status IN ('pending','dispatched'))
```

The `NOT EXISTS` is supported by `tool_calls_pending_idx`.

**Why this predicate has no false positives.** Walk the other ways to be in
`'executing'`. Immediately after `advance` inserts a batch, the rows are
`'pending'` — excluded. Mid-batch, at least one row is `'pending'` or
`'dispatched'` — excluded. A batch that exhausted `MAX_TOOL_ATTEMPTS` set the
session to `'failed'` — excluded. The only state that matches is a closed
batch with no assistant turn after it.

**Why the grace period is small.** `advance`'s first act is `_claim_thinking`,
which moves `executing → thinking`. So the window where this predicate matches
is exactly the gap between the result transaction committing and the
background task claiming — milliseconds on a healthy system. `updated_at` is
the right clock because `record_action_result` bumps it on every accepted
result. A grace of tens of seconds is generous.

**Why it is safe even without the grace period.** If the nudge and the
background task race, `_claim_thinking` picks one. The grace only avoids
pointless work.

**Action:** `advance(session_id)`. Nothing else. The tool messages are already
in the log, so `_load_context` returns the full context including the results,
and the model continues exactly where the background task would have taken it.

---

## 6. `run_command` and double execution — item 4

`--network none` is off the table: the container has to clone, and it has to
reach package registries. So the question is not how to prevent re-execution
but *which* re-executions change an outcome.

### 6.1 The exact sequence where a call runs twice

```
t0   epoch 1 claims call X       attempts=1, status='dispatched', epoch=1
t1   epoch 1 runs `curl -X POST /orders`   -> order 1001 created
t2   network partition. report_result starts retrying, forever.
t3   SandboxNudge.reap_expired sees the heartbeat stop, marks 'dead'
t4   spawn bumps to epoch 2; X requeued to 'pending'
t5   epoch 2 claims X            attempts=2, repeated=true, epoch=2
t6   epoch 2 runs the same curl  -> order 1002 created
t7   epoch 2 reports. Accepted, because epoch 2 is current. X -> 'done'
t8   partition heals. Epoch 1's retry arrives with epoch=1 -> 409 StaleEpoch
```

### 6.2 What control actually ends up with

**Control receives two results and accepts exactly one.** The ledger is
single-result by construction: the accepting UPDATE requires
`status = 'dispatched' AND epoch = current_epoch`, so the first accepted
result closes the row and anything later is either `_already_accepted` (same
epoch, 200 and no change) or `StaleEpoch` (older epoch, 409).

The consequence worth naming is which one survives. **The accepted result is
the second execution's. The first execution's result is discarded, even though
its side effect persists.** So the model is told about order 1002 and never
learns that 1001 exists. The orphan is invisible to the conversation, and it is
invisible because we threw away the only message that mentioned it.

That suggests a cheap improvement, independent of any of the below: when a
stale-epoch result arrives for a call that is already `'done'`, **record it**
rather than dropping it — as an event, or a `superseded_result` column. It does
not prevent anything, but it turns a silent loss into something a human can
read afterwards.

### 6.3 Which commands actually matter

**Safe because they are pure.** `ls`, `cat`, `rg`, `git log`, test runs,
compilers. No external state.

**Safe because of the reset, which is the interesting class.** `echo x >> f`,
`mkdir d`, `mv a b`, `sed -i` — all non-idempotent in the ordinary sense, and
all safe here. The replacement resets the workspace to `last_accepted_sha`
before re-running, and the dead epoch's commit was never accepted, so attempt
two starts from byte-identical bytes to attempt one. Filesystem
non-idempotency is fully covered by the epoch reconciliation that already
exists.

*With one exception.* Anything `.gitignore`d never reaches the remote, so it
is not restored on rebuild. `FORCE_ADD_PATHS` covers `.env`; a local sqlite
file, a `.venv`, a `node_modules` marker are not covered. Attempt two then runs
against a subtly different tree than attempt one. That is a divergence rather
than a double effect, but it is the same root cause and belongs in the same
audit.

**Safe because they are externally idempotent.** `git clone`, `pip install`,
`apt-get install`, `curl` GETs. Wasteful to repeat, correct to repeat. This is
the class that needs the network, which is why the network stays.

**Genuinely unsafe** — and for a coding agent this cohort is much narrower
than "HTTP writes", so it is worth naming precisely rather than gesturing at
`curl -X POST`.

The useful axis is not read versus write. It is **whether the target enforces
uniqueness**, and most developer tooling does, because it was built for humans
who double-click:

| command | second run | why |
|---|---|---|
| `gh pr create` | refused | one PR per head→base pair |
| `npm publish`, `cargo publish` | refused | versions are immutable |
| `gh release create` | refused | the tag already exists |
| `alembic upgrade head` | no-op | version table |
| `docker push :tag` | no-op | same digest |
| `git push` to our own branch | no-op | force-push to the same ref |

All of those fail or no-op *safely*: noisy, non-destructive, and the error
text is usually enough for the model to work out what happened. They are not
the problem.

What is left after removing them is two shapes, and they are the whole cohort:

**Append-only writes**, which have no natural key and so accept every
duplicate. For a coding agent these dominate, because the agent's own workflow
runs them: `gh pr comment`, `gh issue comment`, `gh pr review`, a status
webhook, a Slack or email notification. Duplicate review comments are the
classic artifact of a retried coding agent.

**Auto-incrementing writes**, which manufacture a new identity on each attempt
and so defeat the uniqueness the target would otherwise enforce.
`npm version patch && npm publish` is the sharp example: publish alone is
idempotent, but bumping first makes attempt two a *different* version, so both
land. Same shape as `POST /orders` returning 1001 then 1002. Also here: raw
`psql -c "INSERT ..."` and seed scripts against a shared dev database — which,
under `SANDBOX_RUNTIME=process`, includes the control plane's own Postgres,
because `_start_process` is not a sandbox.

Third, and less about correctness: **triggers** — `gh workflow run`, a deploy
hook, `vercel deploy`. Effect is usually last-write-wins, so the end state is
right, but it burns CI minutes and double-notifies.

**The calibration that matters.** The agent's deliverable is a branch and a
diff, and that is idempotent by construction: a force-push to the same ref
converges no matter how many times it happens. So double execution cannot
corrupt the product. It can only leave litter in side channels — an extra
comment, an extra CI run, an extra published version. That bounds the blast
radius to something a human notices and fixes, rather than something that
silently ships wrong code.

So: the hazard is two shapes of command, inside one window — between a sandbox
becoming unreachable and its replacement finishing the same call — and its
worst outcome is embarrassment rather than a bad merge.

### 6.4 What to do about it

Ordered by cost, not all mutually exclusive.

1. **Make the model the judge. Implemented.** The control plane cannot know
   whether a lost attempt ran, and it cannot know what a duplicate would
   mean, because that depends on what the command was *for*. The model knows
   both. So instead of guessing, it gets told and decides.

   Three parts:

   - `tools.AUDIT_ON_REPEAT` is the set of tools whose repeat is disclosed,
     and it contains only `run_command`. The other three are excluded on
     purpose: a repeated read returns the same bytes, and a repeated
     `write_file` is a full overwrite onto a workspace rebuilt from the last
     accepted commit, so it writes the bytes the first attempt saw. Keeping
     the set this small is what makes the marker worth reading — a notice on
     every repeated read would train the model to skip it.
   - The **system prompt** carries the standing policy, once: what a
     `[repeat]` marker means, that file edits from the lost attempt are
     already undone and must not be re-applied or cleaned up, which effects
     do not roll back (naming the cohort from 6.3), and what to do — look at
     current state, prefer check-then-act over a blind retry, and on anything
     else just carry on.
   - `_tool_message` carries only the facts of the incident, because it is
     prepended to a result the model is reading for its content: that this is
     attempt N, that the previous attempt's sandbox died, and that its output
     was not kept.

   The "do not re-apply or clean up file changes" half is as important as the
   warning. Without it the model spends turns re-verifying a workspace that
   is already correct.

   The `failed` branch of `_tool_message` says the same thing for a call that
   exhausted `MAX_TOOL_ATTEMPTS`, where every attempt was dispatched and so
   every one of them may have run.

2. **Widen the reaper threshold.** The probability of a double execution is
   almost entirely a function of how eagerly `reap_expired` fires relative to
   how long a tool runs. `COMMAND_TIMEOUT_SECONDS` is 120 and the heartbeat
   threshold will be ~30. A sandbox wedged inside a long command still beats
   normally, because the heartbeat is a separate task — so this is mostly about
   partitions, not slow work.

3. **Refuse to re-run instead of disclosing.** The stronger form of 1: on
   requeue of a `run_command`, do not dispatch it again at all. Close it with
   a result saying it may or may not have run, and let the model reissue it if
   it wants to. Trades one guaranteed model turn per incident for never
   executing twice.

   Worth holding off on. Disclosure already routes the decision to the model,
   and 6.3 says most repeats are builds, tests and searches where re-running
   is both free and what the model would ask for anyway. Reach for this if
   real transcripts show the model mishandling the marker.

4. **Egress allowlist instead of `--network none`.** Permit the git remote and
   the package registries, deny the rest. Keeps 6.3's third class working,
   removes most of the fourth. This is the version of network isolation that is
   compatible with the requirement.

5. **A lease.** cursord stops executing after N missed beats instead of working
   through a partition. This is the only option that actually closes the
   window, and `heartbeat_loop` currently documents the opposite choice on
   purpose: "exiting on a failed beat would abandon a tool call that is running
   and about to report". That is a real availability argument. But it is
   exactly the trade being made, and right now it is made implicitly by a
   comment rather than deliberately. Worth a decision either way.

---

## 7. Configuration

New values, in `control/config.py`:

| name | default | meaning |
|---|---|---|
| `NUDGE_INTERVAL_SECONDS` | `15` | how often the loop runs |
| `HEARTBEAT_DEATH_SECONDS` | `30` | three missed beats at cursord's 10s interval |
| `SPAWN_STUCK_SECONDS` | `120` | headroom over `SPAWN_TIMEOUT_SECONDS` |
| `MAX_SANDBOX_LOSSES` | `10` | sandboxes a session may lose before it fails |
| `ADVANCE_GRACE_SECONDS` | `30` | headroom over the caller that normally advances |
| `NUDGE_MAX_ADVANCES` | `8` | concurrent model calls per instance for the nudger |

`ADVANCE_GRACE_SECONDS` is shared by `wake_unanswered` and
`resume_stalled_batch`, which are the same shape of problem: a hand-off inside
a process that died before making it.

`thinking_deadline()` is derived rather than configured:
`GROK_TIMEOUT_SECONDS × GROK_MAX_ATTEMPTS + slack`.

---

## 8. Not covered here

**Spawn-on-demand.** Every nudge above *replaces* a sandbox. Nothing creates
one for a session that woke from idle, so the second user message in a
conversation still produces tool calls with no worker. That is a change to
`advance`, not a nudge, and it is the other half of reactivation.

**`teardown`.** Stopping the container of a session that finished. The
`sandboxes` row reaches `'exited'` or `'dead'` and the process or container is
left to exit on its own.
