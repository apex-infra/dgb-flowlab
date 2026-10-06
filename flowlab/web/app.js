"use strict";
const NS = "http://www.w3.org/2000/svg";
const COLORS = {CONFIRMED: "#38D39F", BROADCAST: "#2E86F5", PLANNED: "#7C93A8"};
const PILL = {COMPLETE: "jade", RUNNING: "jade", WAITING: "amber", APPROVED: "dgb", CONFIGURED: "amber", PAUSED: "coral",
  ERROR: "coral", ABORTED: "coral", RECOVERY: "coral", IDLE: "mute", CREATED: "mute"};
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
const TOKEN = document.querySelector('meta[name="flowlab-token"]').content;
const CONTROL = document.querySelector('meta[name="flowlab-control"]').content === "1";
let last = null, fetchedAt = 0, beads = [], picked = null, sigs = {};

const $ = id => document.getElementById(id);
const dgb = s => { s = Math.round(s); const neg = s < 0 ? "-" : ""; s = Math.abs(s);
  return neg + Math.floor(s / 1e8) + "." + String(s % 1e8).padStart(8, "0"); };
const mk = (tag, attrs, text) => { const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]); if (text != null) e.textContent = text; return e; };
const h = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls;
  if (text != null) e.textContent = text; return e; };
const clock = s => String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
const amountOf = j => j.amount != null ? dgb(j.amount) : (j.planned === "all" ? "entire balance" : dgb(j.planned));
const changed = (key, value) => { const v = JSON.stringify(value); if (sigs[key] === v) return false; sigs[key] = v; return true; };

async function api(action, body) {
  const r = await fetch("/api/do/" + action, {method: "POST", cache: "no-store",
    headers: {"Content-Type": "application/json", "X-Flowlab-Token": TOKEN}, body: JSON.stringify(body || {})});
  const data = await r.json().catch(() => ({error: "unreadable reply"}));
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}
let toastTimer = null;
function toast(msg, bad) {
  const t = $("toast"); t.textContent = msg; t.className = bad ? "bad" : ""; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 9000 : 4000);
}
async function act(action, body, okMsg) {
  try { const d = await api(action, body); if (okMsg) toast(okMsg); poll(); return d; }
  catch (err) { toast(err.message, true); return null; }
}

function layout(names, W) {
  const pad = 120, step = names.length > 1 ? (W - 2 * pad) / (names.length - 1) : 0;
  return Object.fromEntries(names.map((w, i) => [w, {x: pad + i * step, y: 165, i}]));
}

function drawStage(flow, bal, nextSeq) {
  const svg = $("stage"); svg.replaceChildren(); beads = [];
  const names = [flow.source, ...flow.wallets, flow.dest], pos = layout(names, 1000);
  const total = names.reduce((a, w) => a + ((bal && bal[w]) || 0), 0), used = {};
  flow.jobs.forEach(j => {
    const a = pos[j.from], b = pos[j.to]; if (!a || !b) return;
    const planned = j.state === "PLANNED";
    const col = (planned && j.seq === nextSeq) ? "#F0B64A" : (COLORS[j.state] || "#7C93A8");
    let p, lx, ly;

    if (j.from === j.to) {
      const key = "self:" + j.from;
      const k = used[key] = (used[key] || 0) + 1;
      const ht = Math.min(125, 52 + 18 * (k - 1));
      p = mk("path", {
        d: `M${a.x - 16},${a.y - 12} C${a.x - 82},${a.y - ht} ${a.x + 82},${a.y - ht} ${a.x + 16},${a.y - 12}`,
        fill: "none",
        stroke: col,
        "stroke-width": planned ? 1.5 : 2.5,
        "stroke-dasharray": planned ? "5 6" : "none"
      });
      lx = a.x;
      ly = a.y - ht + 8;
    } else {
      const dir = b.i > a.i ? -1 : 1, span = Math.abs(b.i - a.i);
      const key = dir + ":" + Math.min(a.i, b.i) + "-" + Math.max(a.i, b.i);
      const k = used[key] = (used[key] || 0) + 1;
      const ht = Math.min(135, 34 + 30 * span + 22 * (k - 1));
      const mx = (a.x + b.x) / 2;
      p = mk("path", {
        d: `M${a.x},${a.y} Q${mx},${a.y + dir * ht * 2} ${b.x},${b.y}`,
        fill: "none",
        stroke: col,
        "stroke-width": planned ? 1.5 : 2.5,
        "stroke-dasharray": planned ? "5 6" : "none"
      });
      lx = mx;
      ly = a.y + dir * ht;
    }

    svg.appendChild(p);
    svg.appendChild(mk("circle", {cx: lx, cy: ly, r: 11, fill: "#0A1A2B", stroke: col, "stroke-width": 1.5}));
    svg.appendChild(mk("text", {x: lx, y: ly + 4, "text-anchor": "middle", fill: col, "font-size": 12,
      "font-weight": 600}, String(j.seq)));
    if (j.state === "BROADCAST") beads.push({p, len: p.getTotalLength()});
  });
  names.forEach(w => {
    const q = pos[w], v = bal ? bal[w] : null, r = 14 + (total && v ? 24 * Math.sqrt(v / total) : 6);
    const role = w === flow.source ? "#2E86F5" : w === flow.dest ? "#38D39F" : "#7C93A8";
    svg.appendChild(mk("circle", {cx: q.x, cy: q.y, r: r + 6, fill: "none", stroke: role, "stroke-width": 1, opacity: .35}));
    svg.appendChild(mk("circle", {cx: q.x, cy: q.y, r, fill: "#10263B", stroke: role, "stroke-width": 2.5}));
    svg.appendChild(mk("text", {x: q.x, y: q.y + r + 22, "text-anchor": "middle", fill: "#E6EEF5", "font-size": 14,
      "font-weight": 600, stroke: "#0c2034", "stroke-width": 5, "paint-order": "stroke"}, w));
    if (v != null) svg.appendChild(mk("text", {x: q.x, y: q.y + r + 40, "text-anchor": "middle", fill: "#7C93A8",
      "font-size": 12, stroke: "#0c2034", "stroke-width": 4, "paint-order": "stroke"}, dgb(v) + " DGB"));
  });
  beads.forEach(b => { b.c = mk("circle", {r: 6, fill: "#BBD9FF", stroke: "#2E86F5", "stroke-width": 2}); svg.appendChild(b.c); });
}

function frame(ts) {
  beads.forEach(b => { const pt = b.p.getPointAtLength((REDUCED ? 0.5 : (ts / 2600) % 1) * b.len);
    b.c.setAttribute("cx", pt.x); b.c.setAttribute("cy", pt.y); });
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

function renderHops(flow) {
  const box = h("div"), req = flow.required;
  flow.jobs.forEach(j => {
    const row = h("div", "hop");
    row.append(h("span", "n", String(j.seq)), h("span", null, j.from + " → " + j.to),
      h("span", null, amountOf(j) + (j.planned === "all" && j.amount == null ? "" : " DGB")));
    const t = h("span", "t"), pips = h("span", "pips");
    for (let i = 0; i < req; i++) pips.appendChild(h("i", "pip" + (i < (j.confs || 0) ? " on" : "")));
    t.append(h("span", {CONFIRMED: "jade", BROADCAST: "dgb", PLANNED: "mute"}[j.state] || "mute", j.state.toLowerCase()), pips);
    if (j.fee) t.append(h("span", null, "fee " + dgb(j.fee)));
    if (j.txid) { const x = h("span", "mono", j.txid.slice(0, 12)); x.title = j.txid; t.append(x); }
    row.append(t); box.appendChild(row);
  });
  return box;
}

function showTab(name) {
  for (const t of ["monitor", "new", "log"]) $("tab-" + t).hidden = t !== name;
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
}
document.querySelectorAll("#tabs button").forEach(b => { b.onclick = () => showTab(b.dataset.tab); });
$("tab-btn-new").hidden = !CONTROL;

function buildApprove(box, rv, after) {
  box.replaceChildren();
  const need = rv.hash.slice(0, 8), hashLine = h("div", "row");
  hashLine.append(h("span", "mute", "config hash"), h("span", "mono", rv.hash));
  const typed = h("input"), note = h("input"), go = h("button", "primary", "Approve this exact plan");
  typed.placeholder = "type " + need; typed.setAttribute("aria-label", "First 8 characters of the config hash");
  note.placeholder = "note (optional)"; go.disabled = true;
  typed.oninput = () => { go.disabled = typed.value.trim() !== need; };
  go.onclick = async () => {
    if (await act("approve", {exp: rv.exp, hash: rv.hash, note: note.value}, "Approved. Press Start run when you are ready.")) {
      if (after) after();
    }
  };
  const row = h("div", "row"); row.append(typed, note, go);
  box.append(h("h2", null, "Review before approving"), h("pre", "mono", rv.text), hashLine,
    h("div", "mute", "Approval is one-time and pinned to this hash. After it, the run needs no further approvals."), row);
}

async function renderApprove(data) {
  const p = $("panel-approve"), e = data.snapshot && data.snapshot.exp;
  if (!(CONTROL && data.control && e && e.state === "CONFIGURED")) { p.hidden = true; sigs.rv = null; return; }
  if (sigs.rv === e.id) return;
  sigs.rv = e.id;
  try { buildApprove(p, await api("review", {exp: e.id}), () => { sigs.rv = null; }); p.hidden = false; }
  catch (err) { sigs.rv = null; toast(err.message, true); }
}

function renderExperimental(data) {
  const box = $("panel-experimental"), s = data.snapshot;
  if (!s || s.mode !== "experimental") {
    box.hidden = true;
    box.replaceChildren();
    return;
  }

  const jobs = s.flows.flatMap(f => f.jobs);
  const current = [...jobs].reverse().find(j => j.generated);
  const rnd = s.randomization || {};

  box.replaceChildren();
  box.hidden = false;

  const head = h("div", "exp-head");
  head.append(
    h("h2", null, "Experimental engine"),
    h("span", "pill dgb", rnd.model || "unknown model"),
    h("span", "mono mute", "seed " + (rnd.seed != null ? rnd.seed : "-"))
  );
  box.append(head);

  const grid = h("div", "exp-grid");

  const item = (label, value, cls) => {
    const d = h("div", "exp-item");
    d.append(h("span", "mute", label), h("b", cls || null, value));
    return d;
  };

  if (!current) {
    grid.append(
      item("Approved workload", String(s.target_jobs || "-") + " decisions"),
      item("Generated", String(jobs.length)),
      item("Current decision", "not generated yet")
    );
    box.append(grid);
    return;
  }

  const g = current.generated;
  const decision = Number.isInteger(g.decision_index) ? g.decision_index + 1 : current.seq;

  grid.append(
    item("Decision", decision + " / " + (s.target_jobs || "?")),
    item("Route", current.from + " → " + current.to, "mono"),
    item("Planned amount", current.planned === "all" ? "entire balance" : dgb(current.planned) + " DGB"),
    item("Delay", String(current.delay_s || 0) + " s"),
    item("Eligible routes", String(g.eligible_transition_count ?? "-")),
    item("Observed source balance", g.observed_balance_sats != null ? dgb(g.observed_balance_sats) + " DGB" : "-"),
    item("Fee reserve", g.fee_reserve_sats != null ? dgb(g.fee_reserve_sats) + " DGB" : "-"),
    item("Generator", "v" + (g.generator_version ?? "-"))
  );

  box.append(grid);
}

function renderUnresolved(data) {
  const p = $("panel-unresolved"), list = (data.extras && data.extras.unresolved) || [];
  if (!changed("unres", list.map(x => x.id + x.status))) return;
  p.replaceChildren(); p.hidden = !list.length;
  if (!list.length) return;
  p.append(h("h2", null, "An interrupted send needs your answer"),
    h("div", "mute", "A send was interrupted before the engine saw the result. Check the transaction in your node or a block explorer, then record what actually happened. Nothing runs until this is settled."));
  list.forEach(a => {
    const row = h("div", "row"), out = h("select"), txid = h("input"), ev = h("input"), b = h("button", null, "Record");
    out.append(new Option("it was broadcast", "broadcast"), new Option("it was not broadcast", "not_broadcast"));
    txid.placeholder = "txid (if broadcast)"; ev.placeholder = "what you checked";
    b.onclick = () => act("resolve", {action: a.id, outcome: out.value,
      txid: out.value === "broadcast" ? txid.value.trim() : null, evidence: ev.value}, "Recorded");
    row.append(h("span", "mono", "#" + a.id + " " + a.kind + " " + (a.amount_sats != null ? dgb(a.amount_sats) + " DGB " : "")
      + (a.txid ? a.txid.slice(0, 12) : "")), out, txid, ev, b);
    p.appendChild(row);
  });
}

function renderBanner(data) {
  const b = $("banner"), on = !!(data.extras && data.extras.emergency);
  if (!changed("emerg", [on, CONTROL && data.control])) return;
  b.replaceChildren(); b.hidden = !on;
  if (!on) return;
  b.append(h("b", null, "Emergency stop is active."), h("span", null, "Nothing can run until it is cleared."));
  if (CONTROL && data.control) {
    const note = h("input"), clear = h("button", null, "Clear emergency stop");
    note.placeholder = "why is it safe to clear?";
    clear.onclick = () => act("clear_stop", {note: note.value}, "Emergency stop cleared");
    b.append(note, clear);
  }
}

function renderActions(data) {
  const s = data.snapshot, e = s && s.exp, run = data.runner || {};
  const mine = !!(e && run.active && run.exp === e.id);
  if (!changed("actions", [e && e.id, e && e.state, mine, run.active, CONTROL && data.control])) return;
  const box = $("actions"); box.replaceChildren();
  if (!(CONTROL && data.control)) { box.append(h("span", "mute", "read-only")); return; }
  let main = null;
  if (e) {
    if (mine) main = ["Pause run", () => act("halt", {}, "Pausing after this step")];
    else if (run.active) main = null;
    else if (e.state === "APPROVED") main = ["Start run", () => act("run", {exp: e.id}, "Run started")];
    else if (["RUNNING", "WAITING", "COMPLETING"].includes(e.state)) main = ["Continue run", () => act("run", {exp: e.id}, "Run continuing")];
    else if (e.state === "PAUSED") main = ["Resume", async () => { if (await act("resume", {exp: e.id}, "Resumed")) act("run", {exp: e.id}); }];
    else if (e.state === "RECOVERY") main = ["Check and recover", () => act("recover", {exp: e.id}, "Recovery checked")];
  }
  if (main) { const b = h("button", "primary", main[0]); b.onclick = main[1]; box.append(b); }
  const stop = h("button", "danger", "Emergency stop"); let armed = false, timer = null;
  const reset = () => { armed = false; stop.classList.remove("armed"); stop.textContent = "Emergency stop"; };
  stop.onclick = () => {
    if (!armed) { armed = true; stop.classList.add("armed"); stop.textContent = "Click again to stop everything"; timer = setTimeout(reset, 5000); }
    else { clearTimeout(timer); reset(); act("stop", {}, "Emergency stop done"); }
  };
  box.append(stop);
}

function renderExports(data) {
  const e = data.snapshot && data.snapshot.exp;
  if (!changed("exports", [e && e.id])) return;
  const box = $("exports"); box.replaceChildren();
  if (!e) return;
  ["csv", "json"].forEach(f => { const a = h("a", "btn", "Export " + f.toUpperCase());
    a.href = "/api/export?exp=" + encodeURIComponent(e.id) + "&fmt=" + f; a.setAttribute("download", ""); box.append(a); });
}

function renderPicker(data) {
  const list = (data.extras && data.extras.experiments) || [], cur = data.snapshot ? data.snapshot.exp.id : "";
  if (!changed("pick", [list.map(x => x.id + x.state), cur])) return;
  const sel = $("exp-pick"); sel.replaceChildren();
  list.forEach(x => sel.append(new Option(x.id + "  " + x.state + (x.description ? "  " + x.description.slice(0, 30) : ""), x.id)));
  sel.value = cur; sel.hidden = !list.length;
}
$("exp-pick").onchange = ev => { picked = ev.target.value; poll(); };

function renderConsole(data) {
  const lines = data.log || [];
  if (!changed("log", [lines.length, lines.length ? lines[lines.length - 1].line : ""])) return;
  const c = $("console"), stick = c.scrollTop + c.clientHeight >= c.scrollHeight - 30;
  c.textContent = lines.map(x => x.t + "  " + x.line).join("\n");
  if (stick) c.scrollTop = c.scrollHeight;
}

function render(data) {
  last = data; fetchedAt = performance.now();
  window.flowWallets = data.wallets || [];
  renderPicker(data); renderExports(data); renderBanner(data); renderActions(data);
  renderUnresolved(data); renderExperimental(data); renderConsole(data);
  const s = data.snapshot;
  if (!s) { $("state").textContent = "none"; $("state").className = "pill mute"; $("desc").textContent = CONTROL ? "No experiment yet. Open New experiment to create one." : "No experiment yet."; return; }
  const e = s.exp, jobs = s.flows.flatMap(f => f.jobs);
  $("desc").textContent = e.description || "";
  const st = $("state"); st.textContent = e.state; st.className = "pill " + (PILL[e.state] || "mute");
  const confirmed = jobs.filter(j => j.state === "CONFIRMED").length;
  const target = s.mode === "experimental" && Number.isInteger(s.target_jobs) ? s.target_jobs : jobs.length;
  $("m-hops").textContent = confirmed + " / " + target;
  $("m-hops-l").textContent = s.mode === "experimental" ? "decisions confirmed" : "hops confirmed";
  $("m-generated").textContent = s.mode === "experimental" ? jobs.length + " / " + target : String(jobs.length);
  $("m-mode").textContent = s.mode || "deterministic";
  $("m-fees").textContent = dgb(jobs.reduce((a, j) => a + (j.fee || 0), 0));
  const al = $("alert"), bad = ["PAUSED", "ERROR", "ABORTED", "RECOVERY"].includes(e.state);
  al.style.display = bad ? "block" : "none"; al.textContent = bad ? e.state + ": " + (e.state_reason || "") : "";
  const hops = $("hops"), wal = $("wallets"), ev = $("events");
  hops.replaceChildren(); wal.replaceChildren(); ev.replaceChildren();
  const next = jobs.find(j => j.state === "PLANNED");
  if (!s.flows.length) $("stage").replaceChildren();
  s.flows.forEach(f => { hops.appendChild(renderHops(f)); drawStage(f, data.balances, next && next.seq); });
  if (e.state === "CONFIGURED") hops.append(h("div", "mute", "Not approved yet. The plan is shown above the table."));
  const bal = data.balances, tot = bal ? Object.values(bal).reduce((a, v) => a + (v || 0), 0) : 0;
  if (!bal) wal.appendChild(h("div", "mute", "Balances need the node, which is not answering."));
  else for (const w in bal) {
    const r = h("div", "w"), bar = h("div", "bar"), i = h("i");
    i.style.width = (tot && bal[w] ? Math.round(100 * bal[w] / tot) : 0) + "%"; bar.appendChild(i);
    r.append(h("span", null, w), h("span", null, bal[w] == null ? "-" : dgb(bal[w]) + " DGB"), bar); wal.appendChild(r);
  }
  s.events.forEach(x => {
    const d = h("div", "ev"), e1 = x.event.charAt(0) + x.event.slice(1).toLowerCase();
    d.append(h("small", "mono", (x.ts || "").slice(11, 19)),
      h("span", null, e1 + (typeof x.amount_sats === "number" && x.amount_sats ? " · " + dgb(x.amount_sats) + " DGB" : "")));
    ev.appendChild(d);
  });
  $("foot").textContent = "updated " + new Date().toLocaleTimeString() + " · " + (CONTROL && data.control ? "full control" : "read-only") + " · this computer only";
  renderApprove(data); tick();
}

function tick() {
  if (!last || !last.snapshot) return;
  const s = last.snapshot, e = s.exp, finished = ["COMPLETE", "IDLE", "ABORTED"].includes(e.state);
  const t0 = e.started_at ? Date.parse(e.started_at) : null;
  const t1 = e.completed_at ? Date.parse(e.completed_at) : Date.parse(last.server_time) + (performance.now() - fetchedAt);
  $("m-time").textContent = t0 ? clock(Math.max(0, Math.round((t1 - t0) / 1000))) : "-";
  const jobs = s.flows.flatMap(f => f.jobs), waiting = jobs.some(j => j.state === "BROADCAST");
  if (finished) { $("m-next").textContent = "-"; $("m-next-l").textContent = "next hop"; }
  else if (waiting) { $("m-next").textContent = "..."; $("m-next-l").textContent = "waiting for confirmations"; }
  else if (s.next_due_s != null) {
    $("m-next").textContent = clock(Math.max(0, Math.round(s.next_due_s - (performance.now() - fetchedAt) / 1000)));
    $("m-next-l").textContent = "until the next hop";
  } else { $("m-next").textContent = "-"; $("m-next-l").textContent = "next hop"; }
}
setInterval(tick, 500);

async function poll() {
  try {
    const r = await fetch("/api/snapshot" + (picked ? "?exp=" + encodeURIComponent(picked) : ""), {cache: "no-store"});
    render(await r.json());
  } catch (err) { $("foot").textContent = "cannot reach the dashboard server, retrying"; }
}
poll(); setInterval(poll, 2000);
