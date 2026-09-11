/* The client, as the architecture doc describes it: enqueue a task, long-poll
   the event feed, render what comes back. No logic beyond that.

   Served by the control plane itself, so every request here is same-origin and
   there is no CORS to arrange and no base URL to configure. */

const $ = (id) => document.getElementById(id);

// The control plane holds a poll for 25 seconds, so a feed request sitting
// open is the normal case rather than a stall. Everything else is quick.
const RETRY_BASE_MS = 500;
const RETRY_MAX_MS = 5000;

// Nothing more is coming after these. `idle` is not one of them: the agent has
// finished its turn, but a reply would start it again.
const TERMINAL = new Set(["failed", "cancelled"]);

const store = {
  get repoUrl() {
    return localStorage.getItem("repo_url") || "";
  },
  set repoUrl(url) {
    localStorage.setItem("repo_url", url);
  },
  // The control plane has no "list my sessions" route, so the browser keeps
  // its own list. Losing it loses nothing the server needs.
  get sessions() {
    try {
      return JSON.parse(localStorage.getItem("sessions") || "[]");
    } catch {
      return [];
    }
  },
  set sessions(list) {
    localStorage.setItem("sessions", JSON.stringify(list.slice(0, 20)));
  },
};

let current = null; // { id, branch }
let follower = null; // the AbortController of the poll in flight

// ---------- ---------- ----------
// talking to the control plane
// ---------- ---------- ----------
class ControlPlaneError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function call(method, path, body, signal) {
  const response = await fetch(path, {
    method,
    signal,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });

  const text = await response.text();
  let parsed = null;
  try {
    parsed = JSON.parse(text);
  } catch {
    /* an error page, a proxy, something that is not us */
  }

  if (!response.ok) {
    throw new ControlPlaneError(response.status, detailOf(parsed, text, response));
  }
  return parsed;
}

function detailOf(parsed, text, response) {
  const detail = parsed && parsed.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // Pydantic hands back one entry per rejected field.
    return detail.map((e) => `${(e.loc || []).join(".")}: ${e.msg}`).join("; ");
  }
  return text.trim() || response.statusText;
}

// ---------- ---------- ----------
// the feed
// ---------- ---------- ----------
function row(tag, body, className) {
  const feed = $("feed");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;

  const li = document.createElement("li");
  if (className) li.className = className;

  const tagCell = document.createElement("span");
  tagCell.className = "tag";
  tagCell.textContent = tag;

  const bodyCell = document.createElement("span");
  bodyCell.className = "body";
  if (typeof body === "string") {
    bodyCell.textContent = body;
  } else {
    bodyCell.append(...body);
  }

  li.append(tagCell, bodyCell);
  feed.append(li);

  // Only chase the bottom if the reader is already there; scrolling back to
  // read a tool result should not be yanked away by the next event.
  if (atBottom) feed.scrollTop = feed.scrollHeight;
  return li;
}

function argsSummary(args) {
  return Object.entries(args || {})
    .map(([key, value]) =>
      typeof value === "string" && value.length > 60
        ? `${key}=<${value.length} chars>`
        : `${key}=${JSON.stringify(value)}`
    )
    .join(" ");
}

function render(event) {
  const payload = event.payload || {};

  switch (event.type) {
    case "text":
      row("agent", payload.text || "", "agent");
      break;

    case "thinking":
      row("thinking", payload.text || "", "thinking");
      break;

    case "status": {
      const status = payload.status || "?";
      setStatus(status);
      const bad = status === "failed" || status === "cancelled";
      row("status", payload.error ? `${status}: ${payload.error}` : status, bad ? "bad" : "");
      break;
    }

    case "tool_started":
      row("tool", `${payload.name} ${argsSummary(payload.args)}`);
      break;

    case "tool_finished": {
      const parts = [];
      const head = document.createElement("span");
      const exitCode = payload.exit_code || 0;
      head.textContent = payload.name + " ";
      const mark = document.createElement("span");
      mark.className = exitCode ? "bad" : "good";
      mark.textContent = exitCode ? `exit ${exitCode}` : "ok";
      head.append(mark);
      if (payload.commit_sha) head.append(` ${payload.commit_sha.slice(0, 8)}`);
      // At-least-once execution is visible in the transcript for the model;
      // it should be visible here too.
      if (payload.repeated) head.append(" (re-run after a sandbox died)");
      parts.push(head);

      if (payload.result) {
        const output = document.createElement("div");
        output.className = "output";
        output.textContent = payload.result;
        parts.push(output);
      }
      row("tool", parts);
      break;
    }

    case "tool_requeued":
      // A call that was in flight when its sandbox died, handed back to the
      // queue. The next tool_started for it carries the same action_id.
      row(
        "tool",
        `${payload.name} requeued after epoch ${payload.lost_epoch} died` +
          ` (attempt ${payload.attempts})`,
        "bad",
      );
      break;

    default: {
      // The sandbox lifecycle set, which only ever reaches the client. The
      // state goes in the body rather than the tag, which is too narrow for
      // "sandbox_replaced".
      const detail = Object.entries(payload)
        .map(([k, v]) => `${k}=${v}`)
        .join(" ");
      const state = event.type.replace("sandbox_", "");
      row("sandbox", `${state} ${detail}`.trim(), event.type === "sandbox_died" ? "bad" : "");
    }
  }
}

// ---------- ---------- ----------
// the diff
// ---------- ---------- ----------
const shortSha = (sha) => (sha ? sha.slice(0, 8) : "?");

function note(text) {
  const el = document.createElement("div");
  el.className = "diff-note";
  el.textContent = text;
  return el;
}

/* What comes back is a stat and a bounded preview, never the whole patch:
   git lives in the sandbox, and the control plane only keeps what the sandbox
   reported. `url` is how the reader gets to the rest of it. */
function renderDiff(diff) {
  const parts = [];
  const headline = document.createElement("div");
  headline.className = "diff-stat";

  if (!diff.head_sha) {
    // No accepted commit: every tool call so far was a read, or none has
    // finished. Not an error, and not worth a stat line of zeroes.
    headline.textContent = "nothing committed yet, so there is no diff";
    parts.push(headline);
    row("diff", parts);
    return;
  }

  const changed = diff.files_changed === 1 ? "1 file changed" : `${diff.files_changed} files changed`;
  headline.textContent =
    `${changed}, +${diff.additions} −${diff.deletions}` +
    `  ${shortSha(diff.base_sha)}..${shortSha(diff.head_sha)}`;
  parts.push(headline);

  const files = diff.files || [];
  if (files.length) {
    const table = document.createElement("div");
    table.className = "diff-files";
    for (const file of files) {
      const path = document.createElement("span");
      path.className = "path";
      path.textContent = file.path;
      table.append(path);

      if (file.binary) {
        // numstat reports no line counts for a binary file, so there is
        // nothing to put in the two numeric columns.
        const binary = document.createElement("span");
        binary.className = "binary";
        binary.textContent = "binary";
        table.append(binary);
      } else {
        const added = document.createElement("span");
        added.className = "add";
        added.textContent = `+${file.additions}`;
        const removed = document.createElement("span");
        removed.className = "del";
        removed.textContent = `−${file.deletions}`;
        table.append(added, removed);
      }
    }
    parts.push(table);
  }

  // The totals above count every file; the list is capped. Say which.
  if (diff.files_truncated) {
    const hidden = diff.files_changed - files.length;
    parts.push(note(`… ${hidden} more file${hidden === 1 ? "" : "s"}, counted above but not listed`));
  }

  if (diff.url) {
    const link = document.createElement("a");
    link.href = diff.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "view the full patch ↗";
    const wrapper = document.createElement("div");
    wrapper.className = "diff-note";
    wrapper.append(link);
    parts.push(wrapper);
  }

  if (diff.preview) {
    const preview = document.createElement("div");
    preview.className = "output";
    preview.textContent = diff.preview;
    parts.push(preview);
  }

  if (diff.preview_truncated) {
    parts.push(
      note(
        diff.url
          ? "… preview truncated, the link has the rest"
          : "… preview truncated, and this remote has no compare URL, so the preview is all there is"
      )
    );
  }

  row("diff", parts);
}

// ---------- ---------- ----------
// the poll loop
// ---------- ---------- ----------
async function follow(sessionId, after = 0) {
  stopFollowing();
  const controller = new AbortController();
  follower = controller;

  let delay = RETRY_BASE_MS;
  let warned = false;

  while (!controller.signal.aborted) {
    let page;
    try {
      page = await call(
        "GET",
        `/sessions/${sessionId}/events?after=${after}&limit=500`,
        null,
        controller.signal
      );
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof ControlPlaneError && error.status === 404) {
        row("client", `session ${sessionId} is unknown to the control plane`, "bad");
        forget(sessionId);
        return;
      }
      // A control plane restarting mid-poll is expected: the request fails,
      // we ask again, another instance answers.
      if (!warned) {
        row("client", "control plane unreachable, retrying…", "bad");
        warned = true;
      }
      await sleep(delay);
      delay = Math.min(delay * 2, RETRY_MAX_MS);
      continue;
    }

    if (warned) {
      row("client", "reconnected");
      warned = false;
    }
    delay = RETRY_BASE_MS;

    for (const event of page.events) {
      render(event);
      if (event.type === "status" && TERMINAL.has((event.payload || {}).status)) {
        setLive(false);
        return;
      }
    }
    // An empty page means the hold expired. Same cursor, ask again.
    after = page.next_after;
  }
}

function stopFollowing() {
  if (follower) follower.abort();
  follower = null;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------- ---------- ----------
// session selection
// ---------- ---------- ----------
function select(session) {
  current = session;
  $("session-id").textContent = session.id;
  $("branch").textContent = session.branch ? `branch ${session.branch}` : "";
  $("feed").replaceChildren();
  setStatus("…");
  setLive(true);
  drawSessions();
  follow(session.id);
}

function remember(session) {
  const list = store.sessions.filter((s) => s.id !== session.id);
  list.unshift(session);
  store.sessions = list;
  drawSessions();
}

function forget(sessionId) {
  store.sessions = store.sessions.filter((s) => s.id !== sessionId);
  drawSessions();
}

function drawSessions() {
  const list = store.sessions;
  const ul = $("sessions");
  ul.replaceChildren();

  if (!list.length) {
    const li = document.createElement("li");
    li.className = "hint";
    li.textContent = "none yet";
    ul.append(li);
    return;
  }

  for (const session of list) {
    const li = document.createElement("li");
    if (current && session.id === current.id) li.className = "current";

    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "pick";
    pick.textContent = session.prompt || session.id;
    pick.title = `${session.id}\n${session.prompt || ""}`;
    pick.addEventListener("click", () => select(session));

    const drop = document.createElement("button");
    drop.type = "button";
    drop.className = "drop";
    drop.textContent = "×";
    // Worth being explicit: this removes a bookmark, not a session. There is
    // no delete route, and the row in Postgres is untouched.
    drop.title = "Remove from this list. The session itself is not deleted.";
    drop.setAttribute("aria-label", `Remove session ${session.id} from the list`);
    drop.addEventListener("click", () => {
      const wasCurrent = current && current.id === session.id;
      forget(session.id);
      if (wasCurrent) clearPane();
    });

    li.append(pick, drop);
    ul.append(li);
  }
}

/** Back to the state before any session was picked. */
function clearPane() {
  stopFollowing();
  current = null;
  $("session-id").textContent = "no session";
  $("branch").textContent = "";
  $("feed").replaceChildren();
  // Not "idle": that is a status the control plane sends for a session whose
  // turn has ended, and this pane has no session at all.
  setStatus("no session");
  setLive(false);
}

function setStatus(status) {
  const pill = $("status");
  pill.textContent = status;
  pill.className = "pill";
  if (status === "failed" || status === "cancelled") pill.classList.add("bad");
  else if (status === "idle") pill.classList.add("good");
  else if (status !== "no session") pill.classList.add("busy");
}

/** Enable or disable everything that only makes sense with a live session. */
function setLive(live) {
  $("message").disabled = !live;
  $("message-form").querySelector("button").disabled = !live;
  $("diff-btn").disabled = !live;
  $("cancel-btn").disabled = !live;
}

// ---------- ---------- ----------
// toast
// ---------- ---------- ----------
let toastTimer = null;

function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = bad ? "toast bad" : "toast";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 6000);
}

function reportFailure(error) {
  if (error instanceof ControlPlaneError) {
    if (error.status === 501) {
      toast(`the control plane has not built this route yet: ${error.detail}`, true);
    } else if (error.status === 404) {
      toast(`no such session: ${error.detail}`, true);
    } else {
      toast(`control plane said ${error.status}: ${error.detail}`, true);
    }
  } else {
    toast(`cannot reach the control plane: ${error.message}`, true);
  }
}

// ---------- ---------- ----------
// wiring
// ---------- ---------- ----------
$("workspace-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const url = $("repo-url").value.trim();
  if (!url) return;
  store.repoUrl = url;
  noteWorkspace();
  toast("git workspace saved");
});

$("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const prompt = $("prompt").value.trim();
  const repoUrl = store.repoUrl || $("repo-url").value.trim();

  if (!repoUrl) {
    toast("set the git workspace first", true);
    $("repo-url").focus();
    return;
  }
  if (!prompt) return;

  const button = $("enqueue");
  button.disabled = true;
  try {
    const created = await call("POST", "/sessions", { repo_url: repoUrl, prompt });
    const session = {
      id: created.session_id,
      branch: created.branch,
      prompt,
      at: Date.now(),
    };
    remember(session);
    select(session);
    $("prompt").value = "";
  } catch (error) {
    reportFailure(error);
  } finally {
    button.disabled = false;
  }
});

$("message-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const content = $("message").value.trim();
  if (!content || !current) return;

  // Echoed before the request rather than after it. A user message on an idle
  // session advances the loop inside the same request, so awaiting it first
  // would put your line under the reply it prompted. It is echoed at all
  // because the control plane appends user messages without emitting an
  // event: they reach the model, never the feed.
  const echo = row("you", content, "you");
  $("message").value = "";

  try {
    await call("POST", `/sessions/${current.id}/messages`, { content });
  } catch (error) {
    // The line stays, marked, so the text is not lost with the toast.
    echo.classList.add("bad");
    echo.querySelector(".tag").textContent = "not sent";
    reportFailure(error);
  }
});

$("cancel-btn").addEventListener("click", async () => {
  if (!current) return;
  try {
    await call("POST", `/sessions/${current.id}/cancel`);
  } catch (error) {
    reportFailure(error);
  }
});

$("diff-btn").addEventListener("click", async () => {
  if (!current) return;
  try {
    renderDiff(await call("GET", `/sessions/${current.id}/diff`));
  } catch (error) {
    reportFailure(error);
  }
});

function noteWorkspace() {
  const url = store.repoUrl;
  const note = $("repo-note");
  const looksRelative = url && !url.includes("://") && !url.startsWith("git@") && !url.startsWith("/");
  note.hidden = !looksRelative;
  note.textContent = looksRelative
    ? "a relative path is resolved against the control plane's working directory; an absolute path is safer"
    : "";
}

async function checkHealth() {
  const pill = $("health");
  try {
    const body = await call("GET", "/healthz");
    pill.textContent = `control plane · ${body.database}`;
    pill.className = "pill good";
  } catch {
    pill.textContent = "control plane unreachable";
    pill.className = "pill bad";
  }
}

$("repo-url").value = store.repoUrl;
noteWorkspace();
drawSessions();
setLive(false);
checkHealth();

// A session opened in another tab, or left running when this one was closed,
// is picked back up rather than lost.
const [latest] = store.sessions;
if (latest) select(latest);
