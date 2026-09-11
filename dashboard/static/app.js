const $ = (id) => document.getElementById(id);
const number = (value) => value == null ? "—" : Number(value).toLocaleString();
const percent = (value) => value == null ? "—" : `${(value * 100).toFixed(1)}%`;
const rate = (value) => value == null ? "—" : `${Number(value).toFixed(2)}/h`;
const dollars = (value) => value == null ? "—" : `$${Number(value).toFixed(4)}`;
const milliseconds = (value) => value == null ? null : value / 1000;

function duration(seconds) {
  if (seconds == null) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
}

function head(index, title) {
  return `<div class="tile-head"><h3>${title}</h3><span class="index">0${index}</span></div>`;
}

function unavailable(index, title, reason) {
  return `${head(index, title)}
    <span class="unavailable">Not collected</span>
    <p class="definition">${reason}</p>`;
}

function sparkline(series) {
  const values = series.map((row) => row.turns_completed + row.turns_failed);
  const max = Math.max(...values, 1);
  return `<div class="spark" title="Hourly completed and failed outcomes">${
    values.map((value) => `<i style="height:${Math.max(4, value / max * 100)}%"></i>`).join("")
  }</div>`;
}

function panel(source, fallback = {}) {
  return source && typeof source === "object" ? source : fallback;
}

function render(data) {
  const tiles = panel(data.tiles);
  const secondary = panel(data.secondary);
  const activity = panel(tiles.activity);
  $("activity").innerHTML = `${head(1, "Activity and outcomes")}
    <div class="stats">
      <div class="stat"><span>Sessions started</span><strong>${rate(activity.sessions_started_per_hour)}</strong></div>
      <div class="stat"><span>Turns completed</span><strong>${rate(activity.turns_completed_per_hour)}</strong></div>
      <div class="stat"><span>Turns failed</span><strong>${rate(activity.turns_failed_per_hour)}</strong></div>
      <div class="stat"><span>Outcome completion</span><strong>${percent(activity.completion_rate)}</strong></div>
    </div>
    ${sparkline(activity.series)}
    <p class="definition">${activity.definition}</p>`;

  const states = panel(tiles.active_states).states || [];
  const total = states.reduce((sum, row) => sum + row.count, 0);
  $("active-states").innerHTML = `${head(2, "Active sessions by state")}
    <div class="metric">${number(total)}</div>
    <div class="stack">${states.map((row) =>
      `<i style="width:${total ? row.count / total * 100 : 0}%"></i>`).join("")}</div>
    <div class="legend">${states.map((row) =>
      `<div><span class="muted">${row.status}</span><strong>${number(row.count)}</strong></div>`).join("")}</div>
    <p class="definition">${tiles.active_states.definition}</p>`;

  $("turn-duration").innerHTML = unavailable(
    3, "End-to-end turn duration", panel(tiles.turn_duration).reason
  );

  const queue = panel(tiles.queue);
  $("queue").innerHTML = `${head(4, "Pending actions")}
    <div class="metric">${number(queue.pending_count)}</div>
    <div class="stats">
      <div class="stat"><span>Dispatched</span><strong>${number(queue.dispatched_count)}</strong></div>
      <div class="stat"><span>Never dispatched</span><strong>${number(queue.never_dispatched_count)}</strong></div>
      <div class="stat"><span>Oldest never dispatched</span><strong>${duration(queue.oldest_never_dispatched_seconds)}</strong></div>
    </div>
    <p class="definition">${queue.definition || "Open tool_calls still waiting to run."}</p>`;

  const recovery = panel(tiles.recovery);
  $("recovery").innerHTML = `${head(5, "Sandbox recovery")}
    <div class="stats">
      <div class="stat"><span>Deaths / hour</span><strong>${rate(recovery.deaths_per_hour)}</strong></div>
      <div class="stat"><span>Recovery success</span><strong>${percent(recovery.success_rate)}</strong></div>
      <div class="stat"><span>Unresolved</span><strong>${number(recovery.unresolved_sessions)}</strong></div>
    </div>
    <p class="definition">${recovery.definition} Resolved cohort: ${
      number(recovery.recovered_sessions + recovery.failed_sessions)
    }.</p>`;

  const llm = panel(tiles.llm);
  if (!llm.available) {
    $("llm").innerHTML = unavailable(
      6, "LLM p95 and error rate", "No completed model attempts in this window."
    );
  } else {
    const causes = (llm.errors || []).length
      ? llm.errors.map((row) => `${row.outcome}: ${row.count}`).join(" · ")
      : "No errors";
    $("llm").innerHTML = `${head(6, "LLM p95 and error rate")}
      <div class="stats">
        <div class="stat"><span>Latency p95</span><strong>${duration(milliseconds(llm.p95_ms))}</strong></div>
        <div class="stat"><span>Attempt errors</span><strong>${percent(llm.attempt_error_rate)}</strong></div>
        <div class="stat"><span>Final-call errors</span><strong>${percent(llm.final_call_error_rate)}</strong></div>
      </div>
      <p class="definition">${causes}. ${llm.definition} In flight: ${number(llm.in_flight)}; stale: ${number(llm.stale_in_flight)}.</p>`;
  }

  const cost = panel(tiles.cost);
  if (!cost.available) {
    $("cost").innerHTML = unavailable(7, "Cost per completed turn", cost.reason);
  } else {
    $("cost").innerHTML = `${head(7, "Cost per completed turn")}
      <div class="metric">${dollars(cost.cost_per_completed_turn_usd)}</div>
      <div class="stats">
        <div class="stat"><span>Window cost</span><strong>${dollars(cost.estimated_cost_usd)}</strong></div>
        <div class="stat"><span>Tokens in / out</span><strong>${number(cost.input_tokens)} / ${number(cost.output_tokens)}</strong></div>
      </div>
      <p class="definition">${cost.definition}</p>`;
  }

  const repeat = panel(tiles.reexecution);
  $("reexecution").innerHTML = `${head(8, "Re-execution rate")}
    <div class="metric">${percent(repeat.rate)}</div>
    <div class="stats">
      <div class="stat"><span>Repeated</span><strong>${number(repeat.reexecuted_calls)}</strong></div>
      <div class="stat"><span>Dispatched</span><strong>${number(repeat.dispatched_calls)}</strong></div>
    </div>
    <p class="definition">${repeat.definition}</p>`;

  const latency = panel(secondary.latency);
  const spawn = panel(secondary.spawn);
  const beats = panel(secondary.heartbeats);
  const loop = panel(secondary.loop_health);
  $("secondary-grid").innerHTML = `
    <article class="detail"><h3>Dispatch and result latency</h3><dl>
      <dt>Insert → first dispatch p50</dt><dd>${duration(latency.dispatch_p50_seconds)}</dd>
      <dt>Insert → first dispatch p95</dt><dd>${duration(latency.dispatch_p95_seconds)}</dd>
      <dt>Dispatch → result p50</dt><dd>${duration(latency.observed_execution_p50_seconds)}</dd>
      <dt>Dispatch → result p95</dt><dd>${duration(latency.observed_execution_p95_seconds)}</dd>
      <dt>Samples</dt><dd>${number(latency.execution_samples)}</dd>
    </dl></article>
    <article class="detail"><h3>Sandbox spawn</h3><dl>
      <dt>Row → ready p50</dt><dd>${duration(spawn.p50_seconds)}</dd>
      <dt>Row → ready p95</dt><dd>${duration(spawn.p95_seconds)}</dd>
      <dt>Samples</dt><dd>${number(spawn.samples)}</dd>
    </dl></article>
    <article class="detail"><h3>Live heartbeats</h3><dl>
      <dt>Pinging now</dt><dd>${number(beats.live)}</dd>
      <dt>Ready but stale</dt><dd>${number(beats.stale_ready)}</dd>
      <dt>Still spawning</dt><dd>${number(beats.spawning)}</dd>
      <dt>Oldest ready beat</dt><dd>${duration(beats.oldest_ready_heartbeat_seconds)}</dd>
      <dt>Pending actions</dt><dd>${number(queue.pending_count)}</dd>
    </dl></article>
    <article class="detail"><h3>Loop health</h3><dl>
      <dt>Epoch high-water</dt><dd>${number(loop.epoch_high_water)}</dd>
      <dt>Thinking sessions</dt><dd>${number(loop.thinking_sessions)}</dd>
      <dt>Oldest thinking</dt><dd>${duration(loop.oldest_thinking_seconds)}</dd>
      <dt>Stalled executing</dt><dd>${number(loop.stalled_executing_sessions)}</dd>
    </dl></article>`;

  $("freshness").textContent = `Fresh ${new Date(data.generated_at).toLocaleTimeString()} · ${data.window_hours}h`;
}

async function load() {
  try {
    const response = await fetch(`api/overview?hours=${$("window").value}`);
    if (!response.ok) throw new Error(`Dashboard API returned ${response.status}`);
    render(await response.json());
    $("error").hidden = true;
  } catch (error) {
    $("error").textContent = error.message;
    $("error").hidden = false;
    $("freshness").textContent = "Unavailable";
  }
}

$("window").addEventListener("change", load);
load();
setInterval(load, 15_000);
