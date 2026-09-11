-- Local Cloud Agent — the schema, and the only copy of it.
-- Applied by hand, and written to be safe to apply more than once.

-- gen_random_uuid() is core since Postgres 13, so no pgcrypto extension.
-- The bundled pgserver build does not ship one.

-- ---------------------------------------------------------------
-- sessions
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    repo_url            text        NOT NULL,
    branch              text        NOT NULL,
    base_sha            text,                       -- where the sandbox's clone started
    last_accepted_sha   text,                       -- newest SHA from a live epoch

    -- Computed in the sandbox, which is the only side holding a clone, and
    -- deliberately bounded: the full patch stays in the repository and the
    -- client follows a compare link to it. This row is rewritten on every
    -- commit, so nothing unbounded belongs in it.
    diff_preview        text,                       -- first DIFF_PREVIEW_CHARS of base..head
    diff_stat           jsonb,                      -- per-file counts and totals

    -- 'idle' is the resting state and also the signal that the sandbox
    -- should exit: a turn that ends without tool calls has nothing left to
    -- run, and a container kept alive for a conversation that may never
    -- continue is a container the reaper will eventually misjudge. The next
    -- user message spawns a fresh epoch, which is the same rebuild recovery
    -- already performs from last_accepted_sha.
    --
    -- It replaces both of the statuses that used to mean this. 'completed'
    -- was never written by any code path, and 'awaiting_user' was already
    -- documented here as "idle, needs a user message".
    --
    -- Three statuses tell a sandbox to stop: 'idle', 'failed', 'cancelled'.
    -- Only the last two refuse a new user message.
    status              text        NOT NULL DEFAULT 'idle'
                          CHECK (status IN (
                            'idle',            -- no sandbox; waiting on the user
                            'thinking',        -- an instance is inside the LLM call
                            'executing',       -- tool calls outstanding
                            'failed',
                            'cancelled')),

    current_epoch       int         NOT NULL DEFAULT 0,

    -- per-session counters, bumped in the same tx as the rows they number
    message_seq         int         NOT NULL DEFAULT 0,
    event_seq           int         NOT NULL DEFAULT 0,

    thinking_since      timestamptz,                -- set on entry to 'thinking'
    error               text,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- reaper: sessions stuck mid-LLM-call because an instance died
CREATE INDEX IF NOT EXISTS sessions_thinking_idx
    ON sessions (thinking_since)
    WHERE status = 'thinking';

-- ---------------------------------------------------------------
-- sandboxes
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sandboxes (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          uuid        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    epoch               int         NOT NULL,

    container_id        text,                       -- docker id, null until spawned

    -- 'exited' is a sandbox that shut itself down on purpose, reported by
    -- cursord on its way out when the session went idle. Distinct from
    -- 'dead', which is a sandbox that stopped answering and was reaped:
    -- collapsing the two would make every ordinary end-of-turn look like a
    -- crash in this table.
    status              text        NOT NULL DEFAULT 'spawning'
                          CHECK (status IN ('spawning','ready','dead','replaced','exited')),

    last_heartbeat_at   timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (session_id, epoch)
);

-- reaper: find sandboxes whose heartbeat has expired. A sandbox that
-- reported its own exit leaves this index by changing status, so a clean
-- shutdown cannot be read as an expired heartbeat and respawned.
CREATE INDEX IF NOT EXISTS sandboxes_live_heartbeat_idx
    ON sandboxes (last_heartbeat_at)
    WHERE status = 'ready';

-- ---------------------------------------------------------------
-- messages  — the model's context, rebuilt verbatim on every LLM call
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          uuid        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq                 int         NOT NULL,       -- from sessions.message_seq

    role                text        NOT NULL
                          CHECK (role IN ('system','user','assistant','tool')),
    content             text,                       -- null on a pure tool-call turn
    reasoning           text,                       -- thinking, if the API returns it

    -- assistant rows: the raw tool_calls array as the provider returned it,
    -- stored so the context can be replayed byte-identical
    tool_calls          jsonb,

    -- tool rows: which provider call this answers
    provider_call_id    text,

    created_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS messages_session_seq_idx ON messages (session_id, seq);

-- ---------------------------------------------------------------
-- tool_calls  — the execution ledger. Recovery logic lives here.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tool_calls (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),  -- the action_id in the API
    session_id          uuid        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id          uuid        NOT NULL REFERENCES messages(id) ON DELETE CASCADE,

    provider_call_id    text        NOT NULL,       -- id from the LLM response
    name                text        NOT NULL,
    args                jsonb       NOT NULL,

    -- Position in the batch the model asked for, and the dispatch order.
    -- created_at cannot do this job: it defaults to now(), which is the
    -- transaction timestamp, so every call in a batch ties and the order a
    -- parallel batch runs in would be arbitrary.
    ordinal             int         NOT NULL DEFAULT 0,

    status              text        NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','dispatched','done','failed')),

    epoch               int,                        -- sandbox epoch it went to
    attempts            int         NOT NULL DEFAULT 0,

    result              text,
    exit_code           int,
    commit_sha          text,                       -- null when the tree was clean
    repeated            boolean     NOT NULL DEFAULT false,  -- re-dispatched after a death

    dispatched_at       timestamptz,
    completed_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (session_id, provider_call_id)
);

-- next-action poll and the "are we done yet" check
CREATE INDEX IF NOT EXISTS tool_calls_pending_idx
    ON tool_calls (session_id)
    WHERE status IN ('pending','dispatched');

-- dashboard: exact queue age is available before the first dispatch. A
-- requeued call has no pending_since timestamp, so it is deliberately absent.
CREATE INDEX IF NOT EXISTS tool_calls_never_dispatched_pending_idx
    ON tool_calls (created_at)
    WHERE status = 'pending' AND attempts = 0;

-- ---------------------------------------------------------------
-- events  — the UI feed
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    session_id          uuid        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq                 int         NOT NULL,       -- from sessions.event_seq
    -- The sandbox lifecycle set is the client's only window onto a layer it
    -- never talks to. 'sandbox_exited' is the ordinary end of that life,
    -- kept apart from 'sandbox_died' so the feed can say the session
    -- finished and its container left, rather than implying a crash every
    -- time a turn ends.
    -- No 'sandbox_replaced': a replacement is one death and one spawn, and
    -- 'sandbox_died' already carries replaced_by.
    type                text        NOT NULL
                          CHECK (type IN (
                            'thinking','text','status',
                            'tool_started','tool_finished','tool_requeued',
                            'sandbox_spawning','sandbox_ready',
                            'sandbox_died','sandbox_exited')),
    payload             jsonb       NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (session_id, seq)
);

-- dashboard: bounded outcome time series and per-session recovery cohorts.
CREATE INDEX IF NOT EXISTS events_status_history_idx
    ON events (created_at, ((payload ->> 'status')))
    WHERE type = 'status';

CREATE INDEX IF NOT EXISTS events_type_created_idx
    ON events (type, created_at);

CREATE INDEX IF NOT EXISTS events_session_lifecycle_idx
    ON events (session_id, type, created_at);

-- ---------------------------------------------------------------
-- in-place changes
--
-- Everything above is CREATE ... IF NOT EXISTS, which builds a new database
-- and silently skips an existing one. A CHECK constraint that has to change
-- therefore needs saying twice: once in the table above for a fresh
-- database, and once here for the one already running.
--
-- Drop-then-add is what makes this idempotent, and the names are the ones
-- Postgres generates for an inline CHECK, so these find the constraints
-- whether the table was created before this section existed or after.
--
-- Applying this file is a deliberate act — nothing runs it on boot — and it
-- must land with the code that writes the new values, not before it. The
-- control plane now writes 'idle', so this section and the code agree; a
-- deploy that runs the code against an unmigrated database gets a check
-- violation on the first turn that ends without tool calls.
-- ---------------------------------------------------------------

-- sessions gains the two columns the diff is served from. The CREATE TABLE
-- above already declares them, which covers a database built from scratch
-- and does nothing at all for one that already has a sessions table.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS diff_preview text;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS diff_stat    jsonb;

-- tool_calls gains the dispatch order. Existing rows default to 0, which
-- leaves them tied with each other exactly as they already were.
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS ordinal int NOT NULL DEFAULT 0;

-- sessions.status: 'awaiting_user' and 'completed' collapse into 'idle'.
-- The constraint comes off first because the rows cannot hold the new value
-- while the old one is still enforced.
ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_status_check;

UPDATE sessions SET status = 'idle' WHERE status IN ('awaiting_user', 'completed');

ALTER TABLE sessions ALTER COLUMN status SET DEFAULT 'idle';

ALTER TABLE sessions ADD CONSTRAINT sessions_status_check
    CHECK (status IN ('idle','thinking','executing','failed','cancelled'));

-- sandboxes.status: 'exited' joins it, for a sandbox that stopped on its own.
ALTER TABLE sandboxes DROP CONSTRAINT IF EXISTS sandboxes_status_check;

ALTER TABLE sandboxes ADD CONSTRAINT sandboxes_status_check
    CHECK (status IN ('spawning','ready','dead','replaced','exited'));

-- events.type: 'sandbox_exited' joins the lifecycle set, so the clean
-- shutdown has something to say to the client, and 'tool_requeued' so the
-- feed can show a call being handed to a replacement sandbox.
--
-- 'sandbox_replaced' leaves it. It was declared but never written by any code
-- path, and a replacement is already two events: the death, carrying
-- replaced_by, and the spawn of the epoch that took over.
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_type_check;

DELETE FROM events WHERE type = 'sandbox_replaced';

ALTER TABLE events ADD CONSTRAINT events_type_check
    CHECK (type IN ('thinking','text','status',
                    'tool_started','tool_finished','tool_requeued',
                    'sandbox_spawning','sandbox_ready',
                    'sandbox_died','sandbox_exited'));
