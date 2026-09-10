-- Local Cloud Agent — executable schema.
-- Mirrors docs/schema.sql, minus the illustrative queries at the bottom of that
-- file. Idempotent so it can be applied on every boot.

-- gen_random_uuid() is core since Postgres 13, so no pgcrypto extension.
-- The bundled pgserver build does not ship one.

-- ---------------------------------------------------------------
-- sessions
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    repo_url            text        NOT NULL,
    branch              text        NOT NULL,
    base_sha            text,                       -- set at first register
    last_accepted_sha   text,                       -- newest SHA from a live epoch

    status              text        NOT NULL DEFAULT 'awaiting_user'
                          CHECK (status IN (
                            'awaiting_user',   -- idle, needs a user message
                            'thinking',        -- an instance is inside the LLM call
                            'executing',       -- tool calls outstanding
                            'completed',
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
    status              text        NOT NULL DEFAULT 'spawning'
                          CHECK (status IN ('spawning','ready','dead','replaced')),

    last_heartbeat_at   timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (session_id, epoch)
);

-- reaper: find sandboxes whose heartbeat has expired
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

-- ---------------------------------------------------------------
-- events  — the UI feed
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    session_id          uuid        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq                 int         NOT NULL,       -- from sessions.event_seq
    type                text        NOT NULL
                          CHECK (type IN (
                            'thinking','text','status',
                            'tool_started','tool_finished',
                            'sandbox_spawning','sandbox_ready',
                            'sandbox_died','sandbox_replaced')),
    payload             jsonb       NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (session_id, seq)
);
