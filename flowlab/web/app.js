"use strict";
const NS = "http://www.w3.org/2000/svg";
const COLORS = {CONFIRMED: "#38D39F", BROADCAST: "#2E86F5", PLANNED: "#7C93A8"};
const PILL = {COMPLETE: "jade", RUNNING: "jade", WAITING: "amber", APPROVED: "dgb", CONFIGURED: "amber", PAUSED: "coral",
  ERROR: "coral", ABORTED: "coral", RECOVERY: "coral", IDLE: "mute", CREATED: "mute"};
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
const TOKEN = document.querySelector('meta[name="flowlab-token"]').content;
const CONTROL = document.querySelector('meta[name="flowlab-control"]').content === "1";
let last = null, fetchedAt = 0, beads = [], picked = null, sigs = {};
const fundAddresses = {};
const fundLabels = {};
const fundAmounts = {};
const fundCoreLabels = {};
const fundAddressTypes = {};
const fundQRImages = {};
const fundQRUris = {};
const fundQRPending = {};
const fundQRTimers = {};

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
    row.append(
      h("span", "n", String(j.seq)),
      h("span", null, j.from + " → " + (j.to_label || j.to || "-")),
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


let mixerLastRefresh = 0;
let mixerLoading = false;

function mixerElement(tag, className, value) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== undefined && value !== null) {
    element.textContent = String(value);
  }
  return element;
}

function renderMixerLab(data) {
  $("mixer-config-panel").hidden =
    !(CONTROL && data && data.configuration_enabled);
  const status = $("mixer-status");
  const overview = $("mixer-overview");
  const details = $("mixer-details");

  overview.replaceChildren();
  details.replaceChildren();

  if (!data || data.status !== "AVAILABLE") {
    status.textContent =
      "Mixer Lab unavailable — existing schema V7 database required.";
    return;
  }

  status.textContent =
    "Schema V7 valid · Local research database · Read-only";

  const recovery = data.recovery || {};

  const metrics = [
    ["Pools", data.pools.total],
    ["Sessions", data.sessions.total],
    [
      "Eligible sessions",
      recovery.eligible_sessions === null
        ? "Unknown"
        : recovery.eligible_sessions
    ],
    ["Recovery lease", recovery.lease || "UNKNOWN"],
  ];

  for (const [label, value] of metrics) {
    const card = mixerElement("div", "mixer-card");
    card.append(
      mixerElement("span", "mixer-label", label),
      mixerElement("strong", "mixer-value", value)
    );
    overview.append(card);
  }

  const groups = [
    ["Pool states", data.pools.states || {}],
    ["Session states", data.sessions.states || {}],
  ];

  for (const [title, states] of groups) {
    const panel = mixerElement("section", "mixer-panel");
    panel.append(mixerElement("h3", null, title));

    const entries = Object.entries(states);

    if (!entries.length) {
      panel.append(
        mixerElement("p", "mute", "No records")
      );
    }

    for (const [state, count] of entries) {
      const row = mixerElement("div", "mixer-row");
      row.append(
        mixerElement("span", null, state),
        mixerElement("strong", null, count)
      );
      panel.append(row);
    }

    details.append(panel);
  }

  const operations = mixerElement("section", "mixer-panel");

  operations.append(
    mixerElement("h3", null, "Operational Diagnostics")
  );

  const operationalRows = [
    ["Lease availability", recovery.lease],
    ["Node synchronization", recovery.node_sync],
    ["Operational readiness", recovery.operational_readiness],
    ["Financial authorization", "DISABLED"],
    ["Automatic recovery", "DISABLED"],
    ["Transaction broadcasting", "DISABLED"],
  ];

  for (const [label, value] of operationalRows) {
    const row = mixerElement("div", "mixer-row");
    row.append(
      mixerElement("span", null, label),
      mixerElement("strong", null, value || "UNKNOWN")
    );
    operations.append(row);
  }

  details.append(operations);
}



function mixerFormBody(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function mixerCreatePool(event) {
  event.preventDefault();
  const form = event.currentTarget;

  if (!form.reportValidity()) return;

  const body = mixerFormBody(form);

  if (await act(
    "mixer_create_pool",
    body,
    "Research pool created in PAUSED state"
  )) {
    form.reset();
    await pollMixerLab();
  }
}

async function mixerCreateSession(event) {
  event.preventDefault();
  const form = event.currentTarget;

  if (!form.reportValidity()) return;

  const body = mixerFormBody(form);
  body.fee_basis_points = Number(body.fee_basis_points);

  if (await act(
    "mixer_create_session",
    body,
    "Research session created"
  )) {
    form.reset();
    await pollMixerLab();
  }
}

$("mixer-pool-form").addEventListener(
  "submit", mixerCreatePool
);

$("mixer-session-form").addEventListener(
  "submit", mixerCreateSession
);


async function mixerCompleteRegistration(event) {
  event.preventDefault();
  const form = event.currentTarget;

  if (!form.reportValidity()) return;

  const body = mixerFormBody(form);
  const lines = body.payouts
    .split(/\\r?\\n/)
    .map(line => line.trim())
    .filter(Boolean);

  if (lines.length < 1 || lines.length > 10) {
    toast("Registration requires 1-10 payout addresses", true);
    return;
  }

  const payouts = [];

  for (const line of lines) {
    const parts = line.split(",");

    if (parts.length !== 2) {
      toast("Use address,allocation_bps on each line", true);
      return;
    }

    const address = parts[0].trim();
    const amount = parts[1].trim();

    if (
      !address ||
      !/^\\d+$/.test(amount) ||
      Number(amount) < 1 ||
      Number(amount) > 10000
    ) {
      toast("Invalid address or allocation format", true);
      return;
    }

    payouts.push({
      address,
      allocation_bps: Number(amount),
    });
  }

  const total = payouts.reduce(
    (sum, item) => sum + item.allocation_bps, 0
  );

  if (total !== 10000) {
    toast("Payout allocations must total exactly 10000", true);
    return;
  }

  if (new Set(payouts.map(item => item.address)).size !== payouts.length) {
    toast("Duplicate payout addresses are not allowed", true);
    return;
  }

  body.payouts = payouts;

  if (!window.confirm(
    "Seal registration permanently? Payout instructions cannot be changed."
  )) {
    return;
  }

  if (await act(
    "mixer_complete_registration",
    body,
    "Participant registration sealed successfully"
  )) {
    form.reset();
    await pollMixerLab();
  }
}

$("mixer-registration-form").addEventListener(
  "submit", mixerCompleteRegistration
);


async function mixerRequestAddress(event) {
  event.preventDefault();
  const form = event.currentTarget;

  if (!form.reportValidity()) return;

  const body = mixerFormBody(form);

  if (!window.confirm(
    "Request a new deposit address from DigiByte Core? " +
    "The reserve wallet must exist and be loaded."
  )) {
    return;
  }

  if (await act(
    "mixer_request_address",
    body,
    "Deposit-address request processed; check session status"
  )) {
    form.reset();
    await pollMixerLab();
  }
}

$("mixer-address-form").addEventListener(
  "submit", mixerRequestAddress
);

function renderMixerSessions(payload) {
  const status = $("mixer-sessions-status");
  const target = $("mixer-sessions");
  target.replaceChildren();

  if (!payload || payload.status !== "AVAILABLE") {
    status.textContent = "Participant telemetry unavailable.";
    return;
  }

  const sessions = Array.isArray(payload.sessions)
    ? payload.sessions : [];

  status.textContent = sessions.length
    ? `${sessions.length} session records · read-only`
    : "No participant sessions recorded.";

  if (payload.truncated) {
    status.textContent += " · Showing first 50 records";
  }

  for (const session of sessions) {
    const panel = mixerElement("section", "mixer-panel");

    panel.append(mixerElement(
      "h3", "mono", session.session_id || "Unknown session"
    ));

    const rows = [
      ["Pool", session.pool_id],
      ["Pool state", session.pool_state],
      ["Session state", session.session_state],
      ["Lifecycle", session.lifecycle_state],
      ["Address state", session.address_state || "NOT REQUESTED"],
      ["Address recovery",
       session.address_recovery_required
         ? "RECONCILIATION REQUIRED"
         : "NO ADDRESS RECOVERY REQUESTED"],
      ["Observations", session.observation_count],
      ["Supported", session.supported],
      ["Invalidated", session.invalidated],
      ["Recheck required", session.recheck_required],
      ["Research review",
       session.research_review_ready ? "READY" : "NOT READY"],
      ["Recovery", session.recovery_state],
    ];

    for (const [label, value] of rows) {
      const row = mixerElement("div", "mixer-row");
      row.append(
        mixerElement("span", null, label),
        mixerElement("strong", null,
          value === undefined || value === null ? "UNKNOWN" : value)
      );
      panel.append(row);
    }

    // Receiving addresses are public, but only a READY
    // address is eligible to be displayed or copied.
    if (
      session.address_state === "READY" &&
      typeof session.deposit_address === "string" &&
      session.deposit_address.length > 0
    ) {
      const addressRow = mixerElement("div", "mixer-row");
      const addressText = mixerElement(
        "strong", "mono", session.deposit_address
      );
      const copyButton = mixerElement(
        "button", null, "Copy Address"
      );

      copyButton.type = "button";

      copyButton.addEventListener("click", async () => {
        try {
          if (!navigator.clipboard ||
              !navigator.clipboard.writeText) {
            throw new Error("Clipboard unavailable");
          }

          await navigator.clipboard.writeText(
            session.deposit_address
          );

          toast("Verified deposit address copied");
        } catch (error) {
          toast("Could not copy address", true);
        }
      });

      addressRow.append(
        mixerElement("span", null, "Verified receiving address"),
        addressText,
        copyButton
      );

      panel.append(addressRow);

      if (session.deposit_wallet) {
        const walletRow = mixerElement("div", "mixer-row");

        walletRow.append(
          mixerElement("span", null, "Verified reserve wallet"),
          mixerElement("strong", null, session.deposit_wallet)
        );

        panel.append(walletRow);
      }
    }

    if (session.address_recovery_required) {
      const warning = mixerElement(
        "p",
        "mute",
        "ADDRESS RECOVERY REQUIRED — Do not request another " +
        "address or provide a receiving address to a participant " +
        "until the existing request has been reconciled."
      );

      warning.setAttribute("role", "alert");
      panel.append(warning);
    }

    if (session.recovery_state === "ACTION_REQUIRED") {
      const warning = mixerElement(
        "p",
        "mute",
        "DEPOSIT EVIDENCE REVIEW REQUIRED — Review the " +
        "session's observation and reconciliation evidence."
      );

      warning.setAttribute("role", "status");
      panel.append(warning);
    }

    target.append(panel);
  }
}

async function pollMixerLab() {
  if (mixerLoading) return;
  mixerLoading = true;

  try {
    const response = await fetch("/api/mixer-lab", {
      cache: "no-store",
    });

    if (!response.ok) {
      throw new Error("Mixer Lab endpoint unavailable");
    }

    renderMixerLab(await response.json());

    const sessionResponse = await fetch(
      "/api/mixer-lab/sessions", {cache: "no-store"}
    );

    if (!sessionResponse.ok) {
      throw new Error("Session endpoint unavailable");
    }

    renderMixerSessions(await sessionResponse.json());
    mixerLastRefresh = Date.now();
  } catch (_) {
    $("mixer-status").textContent =
      "Mixer Lab connection unavailable.";
    $("mixer-sessions-status").textContent =
      "Participant telemetry unavailable.";
    $("mixer-sessions").replaceChildren();
  } finally {
    mixerLoading = false;
  }
}

function showTab(name) {
  for (const t of ["monitor", "fund", "new", "results", "infrastructure", "mixer", "log"]) $("tab-" + t).hidden = t !== name;
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
  if (name === "mixer") pollMixerLab();
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
  const decisions = jobs.filter(
    j => j.generated && Number.isInteger(j.generated.decision_index)
  );
  const finalizations = jobs.filter(
    j => j.generated && j.generated.phase === "finalization"
  );
  const current = decisions.length ? decisions[decisions.length - 1] : null;
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

  const accounting = s.staged_accounting;

  const appendAccounting = () => {
    if (!accounting) return;

    grid.append(
      item("Approved principal", dgb(accounting.approved_principal_sats) + " DGB"),
      item(
        "Commitment",
        accounting.commitment_status
        + (accounting.committed_principal_sats
          ? " · " + dgb(accounting.committed_principal_sats) + " DGB"
          : "")
      ),
      item("Principal after fees", dgb(accounting.principal_after_fees_sats) + " DGB"),
      item("Cumulative workload", dgb(accounting.cumulative_workload_sats) + " DGB"),
      item("Experiment fees", dgb(accounting.experiment_fees_sats) + " DGB"),
      item("Destination receipts", dgb(accounting.destination_receipts_sats) + " DGB"),
      item(
        "Settlement distributions",
        dgb(accounting.settlement_distributions_sats || 0) + " DGB"
      ),
      item(
        "Reserve return",
        dgb(accounting.reserve_return_sats || 0) + " DGB"
      ),
      item("Reserve commit fee", dgb(accounting.commitment_fee_sats) + " DGB")
    );

    if (accounting.accounting_reconciled === true) {
      grid.append(item("Accounting", "reconciled ✓"));
    } else if (accounting.accounting_reconciled === false) {
      grid.append(
        item(
          "Accounting",
          "mismatch " + dgb(accounting.accounting_delta_sats) + " DGB"
        )
      );
    }
  };

  if (!current) {
    grid.append(
      item("Approved workload", String(s.target_jobs || "-") + " decisions"),
      item("Generated", String(decisions.length)),
      item("Current decision", "not generated yet")
    );
    appendAccounting();
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
    item("Observed sender balance", g.observed_balance_sats != null ? dgb(g.observed_balance_sats) + " DGB" : "-")
  );

  if (accounting) {
    appendAccounting();
  } else {
    grid.append(
      item("Source budget used", g.source_budget_used_sats != null ? dgb(g.source_budget_used_sats) + " DGB" : "-"),
      item("Source budget remaining", g.source_budget_remaining_sats != null ? dgb(g.source_budget_remaining_sats) + " DGB" : "-")
    );
  }

  grid.append(
    item("Fee reserve", g.fee_reserve_sats != null ? dgb(g.fee_reserve_sats) + " DGB" : "-"),
    item("Generator", "v" + (g.generator_version ?? "-")),
    item(
      "Finalization",
      finalizations.length
        ? String(finalizations.filter(j => j.state === "CONFIRMED").length)
          + " / " + String(finalizations.length) + " sweeps confirmed"
        : "pending"
    )
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

function orderedWalletNames(bal, roles) {
  if (!bal) return [];

  const out = [];
  const seen = new Set();

  for (const role of ["reserve", "stage", "workers", "hubs", "destinations"]) {
    const names = roles && Array.isArray(roles[role]) ? roles[role] : [];
    for (const w of names) {
      if (Object.prototype.hasOwnProperty.call(bal, w) && !seen.has(w)) {
        out.push(w);
        seen.add(w);
      }
    }
  }

  // Preserve visibility of any allowlisted wallet that has not yet been
  // assigned a dashboard role.
  for (const w of Object.keys(bal)) {
    if (!seen.has(w)) out.push(w);
  }

  return out;
}

function walletRoleName(wallet, roles) {
  const labels = {
    reserve: "reserve",
    stage: "stage",
    workers: "worker",
    hubs: "hub",
    destinations: "destination"
  };

  for (const role of ["reserve", "stage", "workers", "hubs", "destinations"]) {
    if (roles && Array.isArray(roles[role]) && roles[role].includes(wallet))
      return labels[role];
  }

  return null;
}

function walletRoleGroup(role) {
  return {
    reserve: "Reserve",
    stage: "Stage",
    worker: "Workers",
    hub: "Hubs",
    destination: "Destination"
  }[role] || "Other";
}


function fundPaymentURI(address, amount, label) {
  if (!address) return "";

  const params = [];

  if (amount)
    params.push("amount=" + encodeURIComponent(amount));

  if (label)
    params.push("label=" + encodeURIComponent(label));

  return "digibyte:" + address
    + (params.length ? "?" + params.join("&") : "");
}

function validFundAmount(text) {
  const t = String(text || "").trim();

  if (!t) return true;
  if (!/^\d+(\.\d{1,8})?$/.test(t)) return false;

  const n = Number(t);
  return Number.isFinite(n) && n > 0;
}

async function copyFundText(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    toast(what + " copied.");
  } catch (err) {
    toast("Could not copy " + what.toLowerCase() + ".", true);
  }
}

function scheduleFundQR(wallet, uri) {
  if (!uri) return;

  if (
    fundQRUris[wallet] === uri
    && fundQRImages[wallet]
  )
    return;

  if (fundQRPending[wallet] === uri)
    return;

  if (fundQRTimers[wallet])
    clearTimeout(fundQRTimers[wallet]);

  fundQRTimers[wallet] = setTimeout(async () => {
    delete fundQRTimers[wallet];
    fundQRPending[wallet] = uri;

    try {
      const result = await api(
        "fund_reserve_qr",
        {uri}
      );

      if (fundQRPending[wallet] !== uri)
        return;

      fundQRUris[wallet] = result.uri;
      fundQRImages[wallet] = result.image;
    } catch (err) {
      if (fundQRPending[wallet] === uri) {
        delete fundQRUris[wallet];
        delete fundQRImages[wallet];
        toast(err.message, true);
      }
    } finally {
      if (fundQRPending[wallet] === uri)
        delete fundQRPending[wallet];

      if (last)
        renderFundReserve(last);
    }
  }, 300);
}


function renderFundReserve(data) {
  const box = $("fund-reserve");
  if (!box) return;

  const active = document.activeElement;

  if (
    active
    && box.contains(active)
    && active.dataset
    && active.dataset.fundField
  ) {
    return;
  }
  let restoreField = null;
  let restoreStart = null;
  let restoreEnd = null;

  if (
    active
    && box.contains(active)
    && active.dataset
    && active.dataset.fundField
  ) {
    restoreField = active.dataset.fundField;

    if (
      typeof active.selectionStart === "number"
      && typeof active.selectionEnd === "number"
    ) {
      restoreStart = active.selectionStart;
      restoreEnd = active.selectionEnd;
    }
  }

  const roles = data.wallet_roles || {};
  const reserves = Array.isArray(roles.reserve)
    ? roles.reserve
    : [];

  box.replaceChildren();

  if (!reserves.length) {
    box.append(
      h("div", "fund-card"),
      h("div", "mute", "No reserve-role wallet is configured.")
    );
    return;
  }

  const card = h("div", "fund-card");

  const walletRow = h("div", "fund-line");
  walletRow.append(
    h("span", "mute", "Reserve wallet")
  );

  let walletControl;

  if (reserves.length === 1) {
    walletControl = h("span", "mono fund-wallet", reserves[0]);
  } else {
    walletControl = h("select");

    reserves.forEach(wallet => {
      walletControl.append(new Option(wallet, wallet));
    });

    if (
      box.dataset.wallet
      && reserves.includes(box.dataset.wallet)
    )
      walletControl.value = box.dataset.wallet;

    walletControl.onchange = () => {
      box.dataset.wallet = walletControl.value;
      renderFundReserve(last);
    };
  }

  walletRow.append(walletControl);

  const wallet = reserves.length === 1
    ? reserves[0]
    : walletControl.value;

  box.dataset.wallet = wallet;

  const balance = data.balances
    && typeof data.balances[wallet] === "number"
    ? dgb(data.balances[wallet]) + " DGB"
    : "-";

  const balanceRow = h("div", "fund-line");
  balanceRow.append(
    h("span", "mute", "Current balance"),
    h("span", "mono", balance)
  );

  const labelInput = h("input");
  labelInput.type = "text";
  labelInput.dataset.fundField = "label";
  labelInput.maxLength = 100;
  labelInput.placeholder = "optional Core address label";
  labelInput.value = fundLabels[wallet] || "";

  const labelRow = h("div", "fund-line");
  labelRow.append(
    h("span", "mute", "Label"),
    labelInput
  );

  const amountInput = h("input");
  amountInput.type = "text";
  amountInput.dataset.fundField = "amount";
  amountInput.inputMode = "decimal";
  amountInput.placeholder = "optional DGB amount";
  amountInput.value = fundAmounts[wallet] || "";

  const amountRow = h("div", "fund-line");
  amountRow.append(
    h("span", "mute", "Amount (DGB)"),
    amountInput
  );

  const addressTypeSelect = h("select");
  addressTypeSelect.dataset.fundField = "address_type";

  for (const [value, label] of [
    ["legacy", "Legacy"],
    ["p2sh-segwit", "P2SH-SegWit"],
    ["bech32", "Bech32"],
    ["bech32m", "Bech32m (Taproot-era)"],
  ]) {
    addressTypeSelect.append(
      new Option(label, value)
    );
  }

  addressTypeSelect.value =
    fundAddressTypes[wallet] || "bech32";

  addressTypeSelect.onchange = () => {
    fundAddressTypes[wallet] = addressTypeSelect.value;
  };

  const addressTypeRow = h("div", "fund-line");
  addressTypeRow.append(
    h("span", "mute", "Address type"),
    addressTypeSelect
  );

  const address = fundAddresses[wallet] || null;

  const addressRow = h("div", "fund-address-box");

  if (address) {
    addressRow.append(
      h("span", "mute", "Receiving address"),
      h("div", "mono fund-address", address)
    );
  } else {
    addressRow.append(
      h("span", "mute", "Receiving address"),
      h(
        "div",
        "fund-empty",
        "No receiving address generated in this dashboard session."
      )
    );
  }

  const uriBox = h("div", "fund-address-box");
  const uriValue = h(
    "div",
    "mono fund-address",
    address
      ? fundPaymentURI(
          address,
          amountInput.value.trim(),
          labelInput.value.trim()
        )
      : "-"
  );

  uriBox.append(
    h("span", "mute", "Payment URI"),
    uriValue
  );

  const qrBox = h("div", "fund-qr-box");
  const qrTitle = h("span", "mute", "Payment QR");
  const qrContent = h("div", "fund-qr-content");

  qrBox.append(
    qrTitle,
    qrContent
  );

  const amountError = h("div", "err");
  amountError.hidden = true;

  const controls = h("div", "row");

  const generate = h(
    "button",
    "primary",
    address ? "New address" : "Generate receiving address"
  );

  const copyAddress = h("button", null, "Copy address");
  const copyURI = h("button", null, "Copy URI");
  const updateLabel = h("button", null, "Update Core label");
  const clearRequest = h("button", null, "Clear");

  const sync = () => {
    fundLabels[wallet] = labelInput.value;
    fundAmounts[wallet] = amountInput.value;

    const amountOK = validFundAmount(amountInput.value);

    amountError.hidden = amountOK;
    amountError.textContent = amountOK
      ? ""
      : "Amount must be a positive DGB value with at most 8 decimals.";

    const uri = address
      ? fundPaymentURI(
          address,
          amountInput.value.trim(),
          labelInput.value.trim()
        )
      : "";

    uriValue.textContent = uri || "-";

    qrContent.replaceChildren();

    if (!address) {
      qrContent.append(
        h(
          "div",
          "fund-empty",
          "Generate a receiving address to create a payment QR."
        )
      );
    } else if (!amountOK) {
      qrContent.append(
        h(
          "div",
          "fund-empty",
          "Enter a valid amount to update the payment QR."
        )
      );
    } else if (
      fundQRUris[wallet] === uri
      && fundQRImages[wallet]
    ) {
      const img = document.createElement("img");
      img.className = "fund-qr";
      img.alt = "DigiByte payment QR code";
      img.src = fundQRImages[wallet];
      qrContent.append(img);
    } else {
      qrContent.append(
        h(
          "div",
          "fund-empty",
          "Generating QR..."
        )
      );

      scheduleFundQR(wallet, uri);
    }

    generate.disabled = !(CONTROL && data.control) || !amountOK;
    copyAddress.disabled = !address;
    copyURI.disabled = !address || !amountOK;

    updateLabel.disabled = !(
      CONTROL
      && data.control
      && address
      && labelInput.value !== (fundCoreLabels[wallet] || "")
    );
  };

  labelInput.oninput = sync;
  amountInput.oninput = sync;

  generate.onclick = async () => {
    generate.disabled = true;

    try {
      const result = await api(
        "fund_reserve_address",
        {
          wallet,
          label: labelInput.value.trim(),
          address_type: addressTypeSelect.value,
        }
      );

      fundAddresses[wallet] = result.address;
      fundCoreLabels[wallet] = result.label || "";
      fundAddressTypes[wallet] =
        result.address_type || addressTypeSelect.value;
      fundLabels[wallet] = labelInput.value;
      fundAmounts[wallet] = amountInput.value;

      toast(
        address
          ? "New reserve receiving address generated."
          : "Reserve receiving address generated."
      );

      renderFundReserve(last);
    } catch (err) {
      toast(err.message, true);
      sync();
    }
  };

  copyAddress.onclick = () => {
    if (address)
      copyFundText(address, "Address");
  };

  copyURI.onclick = () => {
    const uri = fundPaymentURI(
      address,
      amountInput.value.trim(),
      labelInput.value.trim()
    );

    if (uri)
      copyFundText(uri, "Payment URI");
  };

  updateLabel.onclick = async () => {
    updateLabel.disabled = true;

    try {
      const result = await api(
        "fund_reserve_label",
        {
          wallet,
          address,
          label: labelInput.value.trim(),
        }
      );

      fundCoreLabels[wallet] = result.label || "";
      toast("Core address label updated.");
      sync();
    } catch (err) {
      toast(err.message, true);
      sync();
    }
  };

  clearRequest.onclick = () => {
    if (fundQRTimers[wallet]) {
      clearTimeout(fundQRTimers[wallet]);
      delete fundQRTimers[wallet];
    }

    delete fundAddresses[wallet];
    delete fundLabels[wallet];
    delete fundAmounts[wallet];
    delete fundCoreLabels[wallet];
    delete fundAddressTypes[wallet];
    delete fundQRImages[wallet];
    delete fundQRUris[wallet];
    delete fundQRPending[wallet];

    toast("Fund Reserve request cleared.");
    renderFundReserve(last);
  };

  controls.append(
    generate,
    copyAddress,
    copyURI,
    updateLabel,
    clearRequest
  );

  if (!(CONTROL && data.control))
    controls.append(
      h(
        "span",
        "mute",
        "Reserve wallet changes are unavailable in read-only mode."
      )
    );

  card.append(
    walletRow,
    balanceRow,
    labelRow,
    amountRow,
    addressTypeRow,
    amountError,
    addressRow,
    uriBox,
    qrBox,
    controls,
    h(
      "div",
      "mute fund-note",
      "Generating a new address stores the current label in DigiByte Core. "
      + "Changing the amount only changes the payment request. Changing the "
      + "label does not modify Core until you press Update Core label. "
      + "Clear only resets this dashboard form; it does not delete an "
      + "address or label from DigiByte Core."
    )
  );

  box.append(card);
  sync();

  if (restoreField) {
    const target = restoreField === "label"
      ? labelInput
      : restoreField === "amount"
        ? amountInput
        : restoreField === "address_type"
          ? addressTypeSelect
          : null;

    if (target) {
      target.focus({preventScroll: true});

      if (
        restoreStart !== null
        && restoreEnd !== null
        && typeof target.setSelectionRange === "function"
      ) {
        target.setSelectionRange(
          Math.min(restoreStart, target.value.length),
          Math.min(restoreEnd, target.value.length)
        );
      }
    }
  }
}


function resultDGB(value) {
  return Number.isFinite(value) ? dgb(value) + " DGB" : "-";
}

function resultNumber(value, digits = 2) {
  return Number.isFinite(value) ? Number(value).toFixed(digits) : "-";
}

function resultPercent(value) {
  return Number.isFinite(value) ? (100 * value).toFixed(1) + "%" : "-";
}

function resultSeconds(value) {
  if (!Number.isFinite(value)) return "-";
  if (value < 60) return resultNumber(value, 1) + " s";
  const minutes = Math.floor(value / 60);
  const seconds = value - minutes * 60;
  return minutes + "m " + resultNumber(seconds, 1) + "s";
}

function resultItem(label, value, cls = "") {
  const item = h("div", "result-item" + (cls ? " " + cls : ""));
  item.append(
    h("span", null, label),
    h("b", null, String(value)),
  );
  return item;
}

function resultSection(title, items) {
  const section = h("section", "result-section");
  const grid = h("div", "result-grid");

  items.forEach(item => {
    grid.appendChild(resultItem(item[0], item[1], item[2] || ""));
  });

  section.append(
    h("h2", null, title),
    grid,
  );

  return section;
}

function renderResults(data) {
  const box = $("results");
  const r = data.results;

  box.replaceChildren();

  if (!r) {
    box.appendChild(
      h("div", "mute", "No experiment results are available.")
    );
    return;
  }

  const summary = r.summary || {};
  const activity = r.activity || {};
  const topology = r.topology || {};
  const amounts = r.amounts || {};
  const timing = r.timing || {};
  const obs = r.observability || {};
  const mixing = r.mixing || {};
  const accounting = r.accounting;

  const mix = h("section", "result-mixing");
  const mixHead = h("div", "result-mixing-head");
  const score = h(
    "b",
    "result-score",
    Number.isFinite(mixing.score) ? String(mixing.score) : "-"
  );
  const label = h(
    "span",
    "pill dgb",
    mixing.label || "-"
  );

  mixHead.append(
    h("div", null, "Mixing / Cleanliness"),
    score,
    h("span", "mute", "/ 100"),
    label,
  );

  mix.append(
    mixHead,
    h(
      "div",
      "result-model mono mute",
      mixing.model || "-"
    ),
    h(
      "div",
      "result-interpretation mute",
      mixing.interpretation || ""
    ),
  );

  box.appendChild(mix);

  box.appendChild(resultSection("Experiment", [
    ["State", summary.state || "-"],
    ["Mode", summary.mode || "-"],
    ["Runtime", resultSeconds(summary.runtime_s)],
    ["Total jobs", activity.total_jobs ?? "-"],
  ]));

  if (accounting) {
    box.appendChild(resultSection("Accounting", [
      [
        "Approved principal",
        resultDGB(accounting.approved_principal_sats),
      ],
      [
        "Committed principal",
        resultDGB(accounting.committed_principal_sats),
      ],
      [
        "Experiment fees",
        resultDGB(accounting.experiment_fees_sats),
      ],
      [
        "Cumulative workload",
        resultDGB(accounting.cumulative_workload_sats),
      ],
      [
        "Destination receipts",
        resultDGB(accounting.destination_receipts_sats),
      ],
      [
        "Settlement distributions",
        resultDGB(accounting.settlement_distributions_sats || 0),
      ],
      [
        "Reserve return",
        resultDGB(accounting.reserve_return_sats || 0),
      ],
      [
        "Reconciliation",
        accounting.accounting_reconciled === true
          ? "reconciled ✓"
          : accounting.accounting_reconciled === false
            ? "mismatch"
            : "pending",
        accounting.accounting_reconciled === true
          ? "jade"
          : accounting.accounting_reconciled === false
            ? "coral"
            : "",
      ],
    ]));
  }

  box.appendChild(resultSection("Activity", [
    ["Confirmed", activity.confirmed ?? 0],
    ["Workload decisions", activity.workload_jobs ?? 0],
    ["Allocation jobs", activity.allocation_jobs ?? 0],
    ["Consolidations", activity.consolidation_jobs ?? 0],
    ["Finalizations", activity.finalization_jobs ?? 0],
    [
      "Terminal distributions",
      activity.terminal_distribution_jobs ?? 0,
    ],
    ["Settlement jobs", activity.settlement_jobs ?? 0],
    ["Reserve return jobs", activity.reserve_return_jobs ?? 0],
  ]));

  box.appendChild(resultSection("Topology", [
    ["Route executions", topology.route_executions ?? 0],
    ["Unique edges", topology.unique_edges ?? 0],
    [
      "Repeated-edge ratio",
      resultPercent(topology.repeated_edge_ratio),
    ],
    [
      "Self-transfer ratio",
      resultPercent(topology.self_transfer_ratio),
    ],
    [
      "Route entropy",
      resultNumber(topology.route_entropy_bits, 3) + " bits",
    ],
    [
      "Normalized route entropy",
      resultNumber(topology.route_entropy_normalized, 3),
    ],
    ["Wallets exercised", topology.wallets_exercised ?? 0],
    [
      "Wallet entropy",
      resultNumber(topology.wallet_entropy_normalized, 3),
    ],
  ]));

  box.appendChild(resultSection("Amounts", [
    ["Total moved", resultDGB(amounts.total_sats)],
    ["Minimum", resultDGB(amounts.min)],
    ["Median", resultDGB(amounts.median)],
    ["Mean", resultDGB(amounts.mean)],
    ["Maximum", resultDGB(amounts.max)],
    ["Standard deviation", resultDGB(amounts.stdev)],
  ]));

  const planned = timing.planned_delay_s || {};
  const gaps = timing.execution_gap_s || {};
  const confirmations = timing.confirmation_s || {};

  box.appendChild(resultSection("Timing", [
    ["Planned delay mean", resultSeconds(planned.mean)],
    ["Planned delay range",
      planned.count
        ? resultSeconds(planned.min) + " → " + resultSeconds(planned.max)
        : "-"
    ],
    ["Execution-gap mean", resultSeconds(gaps.mean)],
    ["Execution-gap median", resultSeconds(gaps.median)],
    ["Confirmation mean", resultSeconds(confirmations.mean)],
    ["Confirmation max", resultSeconds(confirmations.max)],
  ]));

  box.appendChild(resultSection("Observability", [
    ["Route diversity", resultPercent(obs.route_diversity)],
    [
      "Wallet activity diversity",
      resultPercent(obs.wallet_activity_diversity),
    ],
    [
      "Edge distribution diversity",
      resultPercent(obs.edge_distribution_diversity),
    ],
    ["Amount diversity", resultPercent(obs.amount_diversity)],
    ["Timing diversity", resultPercent(obs.timing_diversity)],
    [
      "Repeated-edge ratio",
      resultPercent(obs.repeated_edge_ratio),
    ],
  ]));

  const edges = Array.isArray(topology.edges)
    ? topology.edges
    : [];

  const routes = h("section", "result-section");
  routes.appendChild(h("h2", null, "Route activity"));

  if (!edges.length) {
    routes.appendChild(
      h("div", "mute", "No workload routes have been observed.")
    );
  } else {
    const table = h("div", "result-table");

    table.append(
      h("div", "result-table-head", "Route"),
      h("div", "result-table-head result-count", "Executions"),
    );

    edges.forEach(edge => {
      table.append(
        h(
          "div",
          "mono",
          (edge.from || "-") + " → " + (edge.to || "-")
        ),
        h("div", "result-count", String(edge.count || 0)),
      );
    });

    routes.appendChild(table);
  }

  box.appendChild(routes);

  const wallets = topology.wallet_activity || {};
  const names = Object.keys(wallets);

  const walletSection = h("section", "result-section");
  walletSection.appendChild(h("h2", null, "Wallet activity"));

  if (!names.length) {
    walletSection.appendChild(
      h("div", "mute", "No workload wallet activity has been observed.")
    );
  } else {
    const table = h("div", "result-table");

    table.append(
      h("div", "result-table-head", "Wallet"),
      h("div", "result-table-head result-count", "Touches"),
    );

    names.forEach(name => {
      const role = walletRoleName(
        name,
        data.wallet_roles || {}
      );

      const cell = h("div", "result-wallet");
      cell.append(
        h("span", "mono", name),
        h(
          "span",
          "wallet-role role-" + (role || "other"),
          role || "other"
        ),
      );

      table.append(
        cell,
        h("div", "result-count", String(wallets[name])),
      );
    });

    walletSection.appendChild(table);
  }

  box.appendChild(walletSection);
}

let infrastructureWalletSelected = null;
let infrastructureSubtab = "overview";

function infraRank(value) {
  return Number.isFinite(value)
    ? Math.round(value * 100) + " / 100"
    : "-";
}

function infraRoleLabel(role) {
  return role || "other";
}

function infraRankPosition(rank, total) {
  return Number.isInteger(rank)
    ? "#" + rank + " of " + total
    : "-";
}

function infraPanel(name) {
  const panel = h("div", "infra-subpanel");
  panel.dataset.infraPanel = name;
  panel.hidden = infrastructureSubtab !== name;
  return panel;
}

function showInfrastructureSubtab(name) {
  infrastructureSubtab = name;

  document
    .querySelectorAll(".infra-subtabs button")
    .forEach(button => {
      button.classList.toggle(
        "on",
        button.dataset.infraSubtab === name
      );
    });

  document
    .querySelectorAll("[data-infra-panel]")
    .forEach(panel => {
      panel.hidden = panel.dataset.infraPanel !== name;
    });
}

function renderInfrastructureWallet(data, walletName) {
  const infra = data.infrastructure;
  const box = $("infra-wallet-detail");

  if (!box || !infra) return;

  const wallet = infra.wallets && infra.wallets[walletName];

  box.replaceChildren();

  if (!wallet) {
    box.appendChild(
      h("div", "mute", "Wallet data is unavailable.")
    );
    return;
  }

  const research = wallet.research || {};
  const walletCount = Object.keys(infra.wallets || {}).length;

  const head = h("div", "infra-wallet-head");
  const ident = h("div");

  ident.append(
    h("b", "mono", wallet.name),
    h(
      "span",
      "wallet-role role-" + infraRoleLabel(wallet.role),
      infraRoleLabel(wallet.role)
    )
  );

  head.append(
    ident,
    h(
      "span",
      wallet.available ? "pill jade" : "pill coral",
      wallet.available ? "AVAILABLE" : "UNAVAILABLE"
    )
  );

  box.appendChild(head);

  const rank = h("section", "infra-wallet-rank");
  const rankHead = h("div", "infra-rank-head");

  rankHead.append(
    h("div", null, "Wallet Research Rank"),
    h(
      "b",
      "result-score",
      Number.isFinite(research.score)
        ? String(research.score)
        : "N/A"
    ),
    Number.isFinite(research.score)
      ? h("span", "mute", "/ 100")
      : h("span", "mute", ""),
    h(
      "span",
      "pill dgb",
      research.label || "-"
    )
  );

  rank.appendChild(rankHead);

  box.appendChild(rank);

  box.appendChild(resultSection("Wallet Research Profile", [
    [
      "Balance share",
      resultPercent(research.balance_share),
    ],
    [
      "UTXO share",
      resultPercent(research.utxo_share),
    ],
    [
      "Balance rank",
      infraRankPosition(
        research.balance_rank,
        walletCount
      ),
    ],
    [
      "UTXO-count rank",
      infraRankPosition(
        research.utxo_count_rank,
        walletCount
      ),
    ],
    [
      "Confirmation rank",
      infraRank(research.confirmed_ratio),
    ],
    [
      "UTXO size diversity",
      infraRank(research.utxo_size_diversity),
    ],
    [
      "UTXO evenness",
      infraRank(research.utxo_evenness),
    ],
    [
      "Fragmentation index",
      infraRank(research.fragmentation_index),
    ],
  ]));

  if (!wallet.available) {
    box.appendChild(
      h(
        "div",
        "mute",
        "This configured wallet is not currently available for inspection."
      )
    );
    return;
  }

  box.appendChild(resultSection("Wallet Overview", [
    ["Trusted balance", resultDGB(wallet.trusted_balance_sats)],
    ["Pending balance", resultDGB(wallet.pending_balance_sats)],
    ["Immature balance", resultDGB(wallet.immature_balance_sats)],
    ["Total balance", resultDGB(wallet.total_balance_sats)],
    ["UTXOs", wallet.utxo_count ?? 0],
    ["Confirmed UTXOs", wallet.confirmed_utxo_count ?? 0],
    ["Unconfirmed UTXOs", wallet.unconfirmed_utxo_count ?? 0],
  ]));

  const stats = wallet.utxo_stats || {};

  box.appendChild(resultSection("UTXO Size Profile", [
    ["Smallest", resultDGB(stats.min)],
    ["Median", resultDGB(stats.median)],
    ["Mean", resultDGB(stats.mean)],
    ["Largest", resultDGB(stats.max)],
    ["Standard deviation", resultDGB(stats.stdev)],
  ]));

  const utxos = Array.isArray(wallet.utxos)
    ? wallet.utxos
    : [];

  const section = h("section", "result-section");
  section.appendChild(h("h2", null, "UTXO Inventory"));

  if (!utxos.length) {
    section.appendChild(
      h(
        "div",
        "mute",
        "This wallet currently has no unspent outputs."
      )
    );
  } else {
    const table = h("div", "infra-utxo-table");

    table.append(
      h("div", "result-table-head", "Outpoint"),
      h("div", "result-table-head", "Amount"),
      h(
        "div",
        "result-table-head result-count",
        "Confirmations"
      )
    );

    utxos.forEach(utxo => {
      const txid = utxo.txid || "-";
      const outpoint = h(
        "div",
        "mono infra-outpoint",
        txid.slice(0, 14)
          + "…:"
          + String(utxo.vout ?? "-")
      );

      outpoint.title =
        txid + ":" + String(utxo.vout ?? "-");

      table.append(
        outpoint,
        h("div", null, resultDGB(utxo.amount_sats)),
        h(
          "div",
          "result-count "
            + (utxo.confirmed ? "jade" : "amber"),
          Number.isFinite(utxo.confirmations)
            ? String(utxo.confirmations)
            : utxo.confirmed
              ? "confirmed"
              : "pending"
        )
      );
    });

    section.appendChild(table);
  }

  box.appendChild(section);
}

function renderInfrastructure(data) {
  const box = $("infrastructure");
  const infra = data.infrastructure;

  box.replaceChildren();

  if (!infra) {
    box.appendChild(
      h(
        "div",
        "mute",
        "Infrastructure data requires the local DigiByte node."
      )
    );
    return;
  }

  const overview = infra.overview || {};
  const research = infra.research || {};
  const score = infra.score || {};
  const node = infra.node || {};
  const wallets = infra.wallets || {};

  const subtabs = h("div", "infra-subtabs");

  [
    ["overview", "Overview"],
    ["rankings", "Rankings"],
    ["wallets", "Wallets"],
    ["utxos", "UTXOs"],
  ].forEach(([name, label]) => {
    const button = h(
      "button",
      infrastructureSubtab === name ? "on" : "",
      label
    );

    button.dataset.infraSubtab = name;
    button.onclick = () => showInfrastructureSubtab(name);

    subtabs.appendChild(button);
  });

  box.appendChild(subtabs);

  /*
   * Overview
   */
  const overviewPanel = infraPanel("overview");

  const rank = h("section", "infra-rank");
  const rankHead = h("div", "infra-rank-head");

  rankHead.append(
    h("div", null, "Overall Infra Rank"),
    h(
      "b",
      "result-score",
      Number.isFinite(score.score)
        ? String(score.score)
        : "-"
    ),
    h("span", "mute", "/ 100"),
    h("span", "pill dgb", score.label || "-")
  );

  rank.append(
    rankHead,
    h("div", "mono mute infra-model", score.model || "-"),
    h(
      "div",
      "mute infra-interpretation",
      score.interpretation || ""
    )
  );

  overviewPanel.appendChild(rank);

  overviewPanel.appendChild(
    resultSection("Infrastructure Overview", [
      [
        "Managed balance",
        resultDGB(overview.managed_balance_sats),
      ],
      ["Configured wallets", overview.wallet_count ?? 0],
      [
        "Available wallets",
        overview.available_wallet_count ?? 0,
      ],
      ["Total UTXOs", overview.utxo_count ?? 0],
      [
        "Confirmed UTXOs",
        overview.confirmed_utxo_count ?? 0,
      ],
      [
        "Unconfirmed UTXOs",
        overview.unconfirmed_utxo_count ?? 0,
      ],
      [
        "Node",
        node.available
          ? node.ready
            ? "ready ✓"
            : "not ready"
          : "unavailable",
        node.ready ? "jade" : "coral",
      ],
      ["Block height", node.blocks ?? "-"],
    ])
  );

  overviewPanel.appendChild(
    resultSection("Structural Measurements", [
      [
        "Balance concentration",
        resultPercent(research.balance_concentration),
      ],
      [
        "Balance dispersion",
        resultPercent(research.balance_dispersion),
      ],
      [
        "UTXO concentration",
        resultPercent(research.utxo_concentration),
      ],
      [
        "UTXO dispersion",
        resultPercent(research.utxo_dispersion),
      ],
      [
        "UTXO size diversity",
        resultPercent(research.utxo_size_diversity),
      ],
      [
        "Confirmed UTXO ratio",
        resultPercent(research.confirmed_utxo_ratio),
      ],
    ])
  );

  const overallStats = overview.utxo_stats || {};

  overviewPanel.appendChild(
    resultSection("Overall UTXO Profile", [
      ["Smallest", resultDGB(overallStats.min)],
      ["Median", resultDGB(overallStats.median)],
      ["Mean", resultDGB(overallStats.mean)],
      ["Largest", resultDGB(overallStats.max)],
      [
        "Standard deviation",
        resultDGB(overallStats.stdev),
      ],
    ])
  );

  box.appendChild(overviewPanel);

  /*
   * Rankings
   */
  const rankingsPanel = infraPanel("rankings");

  rankingsPanel.appendChild(
    resultSection("Infrastructure Rankings", [
      [
        "Wallet availability rank",
        infraRank(research.wallet_availability),
      ],
      [
        "Role coverage rank",
        infraRank(research.role_coverage),
      ],
      [
        "Node readiness rank",
        infraRank(
          score.components
            && score.components.node_readiness
        ),
      ],
      [
        "Confirmation rank",
        infraRank(research.confirmed_utxo_ratio),
      ],
      [
        "Balance dispersion rank",
        infraRank(research.balance_dispersion),
      ],
      [
        "UTXO dispersion rank",
        infraRank(research.utxo_dispersion),
      ],
      [
        "UTXO size diversity rank",
        infraRank(research.utxo_size_diversity),
      ],
    ])
  );

  const leaderboard = h("section", "result-section");
  leaderboard.appendChild(
    h("h2", null, "Wallet Research Ranking")
  );

  const rankedWallets = Object.values(wallets).slice().sort(
    (a, b) => {
      const as = a.research && a.research.score;
      const bs = b.research && b.research.score;

      if (Number.isFinite(as) && Number.isFinite(bs)) {
        return bs - as || a.name.localeCompare(b.name);
      }

      if (Number.isFinite(as)) return -1;
      if (Number.isFinite(bs)) return 1;

      return a.name.localeCompare(b.name);
    }
  );

  const rankTable = h("div", "infra-rank-table");

  rankTable.append(
    h("div", "result-table-head", "Wallet"),
    h("div", "result-table-head", "Role"),
    h("div", "result-table-head result-count", "Rank"),
    h("div", "result-table-head result-count", "Balance"),
    h("div", "result-table-head result-count", "UTXOs")
  );

  rankedWallets.forEach(wallet => {
    const wr = wallet.research || {};

    rankTable.append(
      h("div", "mono", wallet.name),
      h("div", null, infraRoleLabel(wallet.role)),
      h(
        "div",
        "result-count",
        Number.isFinite(wr.score)
          ? wr.score + " / 100"
          : "N/A"
      ),
      h(
        "div",
        "result-count",
        resultDGB(wallet.total_balance_sats)
      ),
      h(
        "div",
        "result-count",
        wallet.utxo_count ?? "-"
      )
    );
  });

  leaderboard.appendChild(rankTable);
  rankingsPanel.appendChild(leaderboard);

  box.appendChild(rankingsPanel);

  /*
   * Wallets
   */
  const walletPanel = infraPanel("wallets");

  const walletSection = h("section", "result-section");
  walletSection.appendChild(
    h("h2", null, "Wallet Laboratory")
  );

  const names = orderedWalletNames(
    Object.fromEntries(
      Object.keys(wallets).map(name => [
        name,
        wallets[name].total_balance_sats || 0,
      ])
    ),
    data.wallet_roles || {}
  );

  const tabs = h("div", "infra-wallet-tabs");

  if (
    !infrastructureWalletSelected
    || !wallets[infrastructureWalletSelected]
  ) {
    infrastructureWalletSelected = names[0] || null;
  }

  names.forEach(name => {
    const wallet = wallets[name];

    const button = h(
      "button",
      infrastructureWalletSelected === name
        ? "on"
        : "",
      name
    );

    button.title = infraRoleLabel(wallet.role);

    button.onclick = () => {
      infrastructureWalletSelected = name;

      document
        .querySelectorAll(".infra-wallet-tabs button")
        .forEach(b => {
          b.classList.toggle(
            "on",
            b.textContent === name
          );
        });

      renderInfrastructureWallet(data, name);
    };

    tabs.appendChild(button);
  });

  walletSection.appendChild(tabs);

  const detail = h("div");
  detail.id = "infra-wallet-detail";

  walletSection.appendChild(detail);
  walletPanel.appendChild(walletSection);

  box.appendChild(walletPanel);

  if (infrastructureWalletSelected) {
    renderInfrastructureWallet(
      data,
      infrastructureWalletSelected
    );
  }

  /*
   * Infrastructure-wide UTXOs
   */
  const utxoPanel = infraPanel("utxos");

  const utxoSection = h("section", "result-section");
  utxoSection.appendChild(
    h("h2", null, "Infrastructure UTXO Inventory")
  );

  const allUtxos = [];

  Object.values(wallets).forEach(wallet => {
    (wallet.utxos || []).forEach(utxo => {
      allUtxos.push({
        wallet: wallet.name,
        role: infraRoleLabel(wallet.role),
        ...utxo,
      });
    });
  });

  allUtxos.sort((a, b) => {
    return (
      (b.amount_sats || 0) - (a.amount_sats || 0)
      || a.wallet.localeCompare(b.wallet)
    );
  });

  if (!allUtxos.length) {
    utxoSection.appendChild(
      h(
        "div",
        "mute",
        "No unspent outputs are currently present."
      )
    );
  } else {
    const table = h("div", "infra-all-utxo-table");

    table.append(
      h("div", "result-table-head", "Wallet"),
      h("div", "result-table-head", "Role"),
      h("div", "result-table-head", "Outpoint"),
      h(
        "div",
        "result-table-head result-count",
        "Amount"
      ),
      h(
        "div",
        "result-table-head result-count",
        "Confirmations"
      )
    );

    allUtxos.forEach(utxo => {
      const txid = utxo.txid || "-";
      const outpoint = h(
        "div",
        "mono infra-outpoint",
        txid.slice(0, 12)
          + "…:"
          + String(utxo.vout ?? "-")
      );

      outpoint.title =
        txid + ":" + String(utxo.vout ?? "-");

      table.append(
        h("div", "mono", utxo.wallet),
        h("div", null, utxo.role),
        outpoint,
        h(
          "div",
          "result-count",
          resultDGB(utxo.amount_sats)
        ),
        h(
          "div",
          "result-count "
            + (utxo.confirmed ? "jade" : "amber"),
          Number.isFinite(utxo.confirmations)
            ? String(utxo.confirmations)
            : utxo.confirmed
              ? "confirmed"
              : "pending"
        )
      );
    });

    utxoSection.appendChild(table);
  }

  utxoPanel.appendChild(utxoSection);
  box.appendChild(utxoPanel);

  showInfrastructureSubtab(infrastructureSubtab);
}

function render(data) {
  last = data; fetchedAt = performance.now();
  window.flowWallets = data.wallets || [];
  window.flowWalletRoles = data.wallet_roles || {};
  window.flowPlays = data.plays || [];
  renderPicker(data); renderExports(data); renderBanner(data); renderActions(data);
  renderUnresolved(data); renderExperimental(data); renderConsole(data);
  renderFundReserve(data);
  renderResults(data);
  renderInfrastructure(data);
  const s = data.snapshot;
  if (!s) { $("state").textContent = "none"; $("state").className = "pill mute"; $("desc").textContent = CONTROL ? "No experiment yet. Open New experiment to create one." : "No experiment yet."; return; }
  const e = s.exp, jobs = s.flows.flatMap(f => f.jobs);
  $("desc").textContent = e.description || "";
  const st = $("state"); st.textContent = e.state; st.className = "pill " + (PILL[e.state] || "mute");
  const confirmed = jobs.filter(j => j.state === "CONFIRMED").length;
  const decisions = s.mode === "experimental"
    ? jobs.filter(j => j.generated && Number.isInteger(j.generated.decision_index))
    : [];
  const confirmedDecisions = decisions.filter(j => j.state === "CONFIRMED").length;
  const target = s.mode === "experimental" && Number.isInteger(s.target_jobs) ? s.target_jobs : jobs.length;
  $("m-hops").textContent = s.mode === "experimental"
    ? confirmedDecisions + " / " + target
    : confirmed + " / " + target;
  $("m-hops-l").textContent = s.mode === "experimental" ? "decisions confirmed" : "hops confirmed";
  $("m-generated").textContent = String(jobs.length);
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
  else {
    let lastRole = null;

    for (const w of orderedWalletNames(bal, data.wallet_roles || {})) {
      const role = walletRoleName(w, data.wallet_roles || {});
      const group = walletRoleGroup(role);

      if (role !== lastRole) {
        wal.appendChild(h("div", "wallet-group", group));
        lastRole = role;
      }

      const r = h("div", "w"), bar = h("div", "bar"), i = h("i");
      const ident = h("div", "wallet-ident");
      const name = h("span", "wallet-name mono", w);
      const roleBadge = h(
        "span",
        "wallet-role role-" + (role || "other"),
        role || "other"
      );
      const amount = h(
        "span",
        "wallet-balance mono",
        bal[w] == null ? "-" : dgb(bal[w]) + " DGB"
      );

      if (role) r.classList.add("role-" + role);
      if (bal[w] === 0) r.classList.add("zero");

      ident.append(name, roleBadge);

      i.style.width = (
        tot && bal[w]
        ? Math.round(100 * bal[w] / tot)
        : 0
      ) + "%";

      bar.appendChild(i);
      r.append(ident, amount, bar);
      wal.appendChild(r);
    }
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
setInterval(() => {
  if (!$("tab-mixer").hidden &&
      Date.now() - mixerLastRefresh >= 10000) {
    pollMixerLab();
  }
}, 10000);


// ------------------------------------------------------------
// Mixer Lab Research V1 — synthetic experiment interface
// ------------------------------------------------------------


// ------------------------------------------------------------
// Mixer Lab Research V1 — public synthetic UTXO visualization
// ------------------------------------------------------------

function renderMixerResearchGraph(graph) {
  const panel = document.getElementById("mixer-research-graph-panel");
  const svg = document.getElementById("mixer-research-graph");
  const telemetry = document.getElementById(
    "mixer-research-graph-telemetry"
  );
  const selection = document.getElementById(
    "mixer-research-graph-selection"
  );

  if (!panel || !svg || !telemetry || !selection) return;

  panel.hidden = true;
  svg.replaceChildren();
  telemetry.replaceChildren();

  if (!graph || graph.kind !== "mixer_public_utxo_graph" ||
      graph.execution !== "simulation_only" ||
      !Array.isArray(graph.nodes) ||
      !Array.isArray(graph.edges)) {
    return;
  }

  const summary = graph.summary || {};
  const transactions = graph.nodes.filter(
    node => node.kind === "transaction"
  );

  if (!transactions.length) {
    selection.textContent = "No synthetic transactions to display.";
    return;
  }

  const NS = "http://www.w3.org/2000/svg";
  const WIDTH = Math.max(1000, 320 + transactions.length * 230);
  const HEIGHT = 820;
  const TX_Y = 405;

  const makeSVG = (tag, attributes) => {
    const element = document.createElementNS(NS, tag);
    Object.entries(attributes || {}).forEach(([name, value]) => {
      element.setAttribute(name, String(value));
    });
    return element;
  };

  const identifierHash = identifier => {
    let hash = 2166136261;
    for (let i = 0; i < identifier.length; i++) {
      hash ^= identifier.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  };

  const txPositions = new Map();
  const outputProducers = new Map();

  transactions.forEach((tx, index) => {
    txPositions.set(tx.id, {
      x: 160 + index * 230,
      y: TX_Y
    });
  });

  graph.edges.forEach(edge => {
    if (edge.kind === "create") {
      outputProducers.set(edge.target, edge.source);
    }
  });

  const inputConsumers = new Map();

  graph.edges.forEach(edge => {
    if (edge.kind === "spend" &&
        !inputConsumers.has(edge.source)) {
      inputConsumers.set(edge.source, edge.target);
    }
  });

  const positions = new Map(txPositions);

  graph.nodes.forEach(node => {
    if (node.kind !== "utxo") return;

    const producer = outputProducers.get(node.id);
    const consumer = inputConsumers.get(node.id);

    const producerPosition = producer ?
      txPositions.get(producer) : null;

    const consumerPosition = consumer ?
      txPositions.get(consumer) : null;

    const anchor = producerPosition || consumerPosition || {
      x: 160, y: TX_Y
    };

    const lane = identifierHash(node.id) % 12;

    positions.set(node.id, {
      x: producerPosition ? anchor.x + 82 : anchor.x - 82,
      y: 65 + lane * 62
    });
  });

  const defs = makeSVG("defs");

  const marker = makeSVG("marker", {
    id: "mixer-research-arrow",
    markerWidth: 9,
    markerHeight: 9,
    refX: 8,
    refY: 4,
    orient: "auto",
    markerUnits: "strokeWidth"
  });

  marker.appendChild(makeSVG("path", {
    d: "M0,0 L8,4 L0,8 Z",
    fill: "#6c9ec4"
  }));

  defs.appendChild(marker);
  svg.appendChild(defs);

  svg.setAttribute("viewBox", `0 0 ${WIDTH} ${HEIGHT}`);
  svg.setAttribute("width", WIDTH);
  svg.setAttribute("height", HEIGHT);

  const edgeLayer = makeSVG("g", {
    class: "mixer-graph-edges"
  });

  const nodeLayer = makeSVG("g", {
    class: "mixer-graph-nodes"
  });

  const edgeElements = [];
  const nodeElements = [];

  const shortID = identifier =>
    identifier.length > 22 ?
      identifier.slice(0, 12) + "…" + identifier.slice(-6) :
      identifier;

  graph.edges.forEach(edge => {
    const from = positions.get(edge.source);
    const to = positions.get(edge.target);

    if (!from || !to) return;

    const bend = Math.max(36, Math.abs(to.x - from.x) / 2);

    const path = makeSVG("path", {
      d: `M${from.x},${from.y} C${from.x + bend},${from.y} ` +
         `${to.x - bend},${to.y} ${to.x},${to.y}`,
      class: "mixer-graph-edge mixer-graph-edge-" + edge.kind,
      "marker-end": "url(#mixer-research-arrow)",
      "data-source": edge.source,
      "data-target": edge.target
    });

    const title = makeSVG("title");
    title.textContent =
      edge.kind.toUpperCase() + ": " +
      edge.source + " → " + edge.target;

    path.appendChild(title);
    edgeLayer.appendChild(path);
    edgeElements.push({element: path, edge});
  });

  const inspectTransaction = tx => {
    const related = new Set([tx.id]);

    graph.edges.forEach(edge => {
      if (edge.source === tx.id) related.add(edge.target);
      if (edge.target === tx.id) related.add(edge.source);
    });

    edgeElements.forEach(({element, edge}) => {
      const active = (
        edge.source === tx.id || edge.target === tx.id
      );

      element.classList.toggle("is-active", active);
      element.classList.toggle("is-muted", !active);
    });

    nodeElements.forEach(({element, node}) => {
      const active = related.has(node.id);
      element.classList.toggle("is-active", active);
      element.classList.toggle("is-muted", !active);
    });

    selection.textContent =
      "Selected " + tx.id +
      " · Inputs: " + tx.input_count +
      " · Outputs: " + tx.output_count +
      " · Fee: " + tx.fee_sats + " sats";
  };

  graph.nodes.forEach(node => {
    const position = positions.get(node.id);

    if (!position) return;

    const isTx = node.kind === "transaction";

    const group = makeSVG("g", {
      class: "mixer-graph-node " +
        (isTx ? "mixer-graph-tx" : "mixer-graph-utxo"),
      transform: `translate(${position.x},${position.y})`
    });

    if (isTx) {
      group.appendChild(makeSVG("rect", {
        x: -47, y: -21,
        width: 94, height: 42,
        rx: 9
      }));
    } else {
      group.appendChild(makeSVG("circle", {
        r: 11
      }));
    }

    const label = makeSVG("text", {
      x: 0,
      y: isTx ? 5 : -19,
      "text-anchor": "middle"
    });

    label.textContent = isTx ?
      "TX " + node.sequence : shortID(node.id);

    group.appendChild(label);

    const title = makeSVG("title");

    title.textContent = isTx ?
      node.id + " · " + node.input_count +
        " inputs · " + node.output_count +
        " outputs · Fee " + node.fee_sats + " sats" :
      node.id + " · " + node.value_sats + " sats";

    group.appendChild(title);

    if (isTx) {
      group.setAttribute("tabindex", "0");
      group.setAttribute("role", "button");
      group.setAttribute(
        "aria-label", "Inspect transaction " + node.id
      );

      group.addEventListener(
        "click", () => inspectTransaction(node)
      );

      group.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          inspectTransaction(node);
        }
      });
    }

    nodeLayer.appendChild(group);
    nodeElements.push({element: group, node});
  });

  svg.append(edgeLayer, nodeLayer);

  const makeTelemetry = (label, value) => {
    const box = document.createElement("div");
    box.className = "mixer-research-graph-stat";

    const heading = document.createElement("span");
    heading.textContent = label;

    const count = document.createElement("strong");
    count.textContent = String(value ?? 0);

    box.append(heading, count);
    telemetry.appendChild(box);
  };

  makeTelemetry(
    "Visible transactions", summary.visible_transactions
  );

  makeTelemetry(
    "Total transactions", summary.total_transactions
  );

  makeTelemetry("UTXO nodes", summary.visible_utxos);

  makeTelemetry(
    "Spend edges", summary.visible_spend_edges
  );

  makeTelemetry(
    "Create edges", summary.visible_create_edges
  );

  makeTelemetry(
    "Graph truncated", summary.truncated ? "YES" : "NO"
  );

  selection.textContent =
    "Displaying the first campaign seed, position " +
    String(summary.campaign_seed_position ?? 0) +
    ". Select any transaction to inspect its connections.";

  panel.hidden = false;
}


// ------------------------------------------------------------
// Mixer Lab Research V1 — analytics visualization
// ------------------------------------------------------------

function renderMixerResearchAnalytics(response) {
  const root = document.getElementById("mixer-research-analytics");
  const summaryBox = document.getElementById("mixer-analytics-summary");
  const classification = document.getElementById(
    "mixer-analytics-classification"
  );
  const precisionBox = document.getElementById(
    "mixer-analytics-precision"
  );
  const scenarioBox = document.getElementById(
    "mixer-analytics-scenarios"
  );

  if (!root || !summaryBox || !classification ||
      !precisionBox || !scenarioBox) return;

  summaryBox.replaceChildren();
  classification.replaceChildren();
  precisionBox.replaceChildren();
  scenarioBox.replaceChildren();

  const stats = response?.statistics;
  const aggregate = response?.aggregate;

  if (!stats || !aggregate || !stats.counts || !stats.precision) {
    root.hidden = true;
    return;
  }

  const counts = stats.counts;
  const p = stats.precision;

  const percent = value =>
    typeof value === "number" && Number.isFinite(value) ?
      (value * 100).toFixed(2) + "%" : "N/A";

  const numeric = value =>
    typeof value === "number" && Number.isFinite(value) ?
      value.toLocaleString() : "N/A";

  const card = (label, value) => {
    const box = document.createElement("div");
    box.className = "mixer-analytics-stat";

    const title = document.createElement("span");
    title.textContent = label;

    const number = document.createElement("strong");
    number.textContent = value;

    box.append(title, number);
    return box;
  };

  const bar = (label, value, max, tone) => {
    const row = document.createElement("div");
    row.className = "mixer-analytics-bar-row";

    const heading = document.createElement("div");
    heading.className = "mixer-analytics-bar-heading";

    const name = document.createElement("span");
    name.textContent = label;

    const count = document.createElement("strong");
    count.textContent = numeric(value);

    heading.append(name, count);

    const track = document.createElement("div");
    track.className = "mixer-analytics-track";

    const fill = document.createElement("div");
    fill.className = "mixer-analytics-fill " + tone;

    const valid = typeof value === "number" &&
      Number.isFinite(value) && value >= 0 &&
      typeof max === "number" && Number.isFinite(max) && max > 0;

    fill.style.width = valid ?
      Math.min(100, value / max * 100).toFixed(2) + "%" : "0%";

    track.appendChild(fill);
    row.append(heading, track);

    return row;
  };

  summaryBox.append(
    card("Predictions", numeric(aggregate.prediction_count)),
    card("True positives", numeric(counts.tp)),
    card("False positives", numeric(counts.fp)),
    card("Micro precision", percent(p.micro))
  );

  const positiveTotal = Number(counts.tp || 0) +
    Number(counts.fp || 0);

  classification.append(
    bar("True positives", counts.tp, positiveTotal, "positive"),
    bar("False positives", counts.fp, positiveTotal, "negative")
  );

  precisionBox.append(
    card("Micro precision", percent(p.micro)),
    card("Mean seed precision", percent(p.mean)),
    card("Population std. deviation", percent(p.population_stddev)),
    card("Minimum seed precision", percent(p.minimum)),
    card("Maximum seed precision", percent(p.maximum)),
    card("Defined seeds", numeric(p.defined_seeds)),
    card("Undefined seeds", numeric(p.undefined_seeds))
  );

  Object.entries(response.by_scenario || {}).forEach(([name, item]) => {
    const panel = document.createElement("div");
    panel.className = "mixer-analytics-scenario";

    const heading = document.createElement("div");
    heading.className = "mixer-analytics-bar-heading";

    const label = document.createElement("span");
    label.textContent = name.replaceAll("_", " ");

    const value = document.createElement("strong");
    value.textContent = percent(item.metrics?.precision);

    heading.append(label, value);

    const track = document.createElement("div");
    track.className = "mixer-analytics-track";

    const fill = document.createElement("div");
    fill.className = "mixer-analytics-fill scenario";

    const precision = item.metrics?.precision;

    fill.style.width =
      typeof precision === "number" && Number.isFinite(precision) ?
        Math.max(0, Math.min(100, precision * 100)).toFixed(2) + "%" :
        "0%";

    track.appendChild(fill);

    const details = document.createElement("small");
    details.textContent =
      numeric(item.transaction_count) + " transactions · " +
      numeric(item.prediction_count) + " predictions";

    panel.append(heading, track, details);
    scenarioBox.appendChild(panel);
  });

  root.hidden = false;
}

function initMixerResearchForm() {
  const form = document.getElementById("mixer-research-form");
  if (!form) return;

  const status = document.getElementById("mixer-research-status");
  const button = document.getElementById("mixer-research-run");
  const results = document.getElementById("mixer-research-results");
  const metrics = document.getElementById("mixer-research-metrics");
  const scenarios = document.getElementById("mixer-research-scenarios");

  // The existing server rejects all POST actions in read-only mode.
  // Hide execution controls there, rather than presenting a broken form.
  if (!CONTROL) {
    form.hidden = true;
    status.textContent = "Research execution is disabled in read-only mode.";
    return;
  }

  const integer = (value, minimum, maximum, label) => {
    const text = String(value).trim();
    if (!/^(0|[1-9][0-9]*)$/.test(text)) {
      throw new Error(label + " must be a whole number.");
    }
    const number = Number(text);
    if (!Number.isSafeInteger(number) || number < minimum ||
        number > maximum) {
      throw new Error(label + " is outside its permitted range.");
    }
    return number;
  };

  const metricText = value => {
    if (value === null || value === undefined) return "N/A";
    if (typeof value === "number") {
      if (!Number.isFinite(value)) return "N/A";
      return Number.isInteger(value) ?
        value.toLocaleString() : value.toFixed(4);
    }
    return String(value);
  };

  const addMetric = (container, label, value) => {
    const box = document.createElement("div");
    box.className = "mixer-research-metric";

    const title = document.createElement("span");
    title.textContent = label.replaceAll("_", " ");

    const number = document.createElement("strong");
    number.textContent = metricText(value);

    box.append(title, number);
    container.appendChild(box);
  };

  const addSection = (container, title, values) => {
    if (!values || typeof values !== "object" || Array.isArray(values)) {
      return;
    }

    const heading = document.createElement("h4");
    heading.textContent = title;
    container.appendChild(heading);

    const grid = document.createElement("div");
    grid.className = "mixer-research-metrics";

    Object.entries(values).forEach(([key, value]) => {
      if (value === null || typeof value !== "object") {
        addMetric(grid, key, value);
      }
    });

    container.appendChild(grid);
  };

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (button.disabled) return;

    results.hidden = true;
    document.getElementById("mixer-research-analytics").hidden = true;
    metrics.replaceChildren();
    scenarios.replaceChildren();
    document.getElementById(
      "mixer-research-graph-panel"
    ).hidden = true;

    let request;

    try {
      const fields = new FormData(form);
      const rawSeeds = String(fields.get("seeds") || "").split(",");

      if (rawSeeds.length < 1 || rawSeeds.length > 8) {
        throw new Error("Enter between 1 and 8 seeds.");
      }

      const seeds = rawSeeds.map(value =>
        integer(value, 0, Number.MAX_SAFE_INTEGER, "Seed")
      );

      if (new Set(seeds).size !== seeds.length) {
        throw new Error("Experiment seeds must be unique.");
      }

      const weights = {
        single_owner: integer(
          fields.get("single_owner"), 0, 100, "Single-owner weight"
        ),
        collaborative: integer(
          fields.get("collaborative"), 0, 100, "Collaborative weight"
        ),
        mixed_ownership: integer(
          fields.get("mixed_ownership"), 0, 100, "Mixed weight"
        )
      };

      if (Object.values(weights).reduce((a, b) => a + b, 0) !== 100) {
        throw new Error("Scenario weights must total exactly 100%.");
      }

      request = {
        seeds,
        transaction_count: integer(
          fields.get("transaction_count"), 1, 1000, "Transaction count"
        ),
        max_inputs: integer(
          fields.get("max_inputs"), 3, 12, "Maximum inputs"
        ),
        scenario_weights: weights,
        matched_input_profile: fields.has("matched_input_profile")
      };

    } catch (error) {
      status.textContent = error.message;
      return;
    }

    button.disabled = true;
    status.textContent = "Running synthetic research campaign...";

    try {
      // api() uses the existing authenticated /api/do/ transport.
      const response = await api("mixer-research-run", request);

      if (response.kind !== "mixer_dashboard_research_result" ||
          response.execution !== "simulation_only") {
        throw new Error("Unexpected research response.");
      }

      addMetric(
        metrics, "Transactions",
        response.aggregate.transaction_count
      );
      addMetric(
        metrics, "Predictions",
        response.aggregate.prediction_count
      );
      addMetric(
        metrics, "Seed count",
        response.aggregate.seed_count
      );

      addSection(metrics, "Aggregate counts", response.aggregate.counts);
      addSection(metrics, "Aggregate metrics", response.aggregate.metrics);
      addSection(
        metrics, "Statistical precision",
        response.statistics.precision
      );

      Object.entries(response.by_scenario || {}).forEach(([name, item]) => {
        const panel = document.createElement("section");
        panel.className = "mixer-research-scenario";
        addSection(panel, name, {
          transaction_count: item.transaction_count,
          prediction_count: item.prediction_count
        });
        addSection(panel, "Counts", item.counts);
        addSection(panel, "Metrics", item.metrics);
        scenarios.appendChild(panel);
      });

      renderMixerResearchAnalytics(response);
      renderMixerResearchGraph(response.graph);

      results.hidden = false;
      status.textContent =
        "Synthetic campaign complete. Results are simulation-only.";

    } catch (error) {
      status.textContent = "Research failed: " + error.message;
    } finally {
      button.disabled = false;
    }
  });
}

if (document.readyState === "loading") {
  document.addEventListener(
    "DOMContentLoaded", initMixerResearchForm, {once: true}
  );
} else {
  initMixerResearchForm();
}
