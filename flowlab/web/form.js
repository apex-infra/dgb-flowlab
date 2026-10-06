"use strict";
// New experiment form. It only builds a plain config (explicit hops or the repeat shorthand);
// the server validates it with the same rules as the command line. There is no randomness here.

function toSats(text, what) {
  const t = String(text).trim();
  if (!/^\d+(\.\d{1,8})?$/.test(t)) throw new Error(what + ": enter a DGB amount such as 2 or 0.5 (up to 8 decimals)");
  const [i, f = ""] = t.split(".");
  const sats = Number(i) * 1e8 + Number((f + "00000000").slice(0, 8));
  if (!Number.isSafeInteger(sats) || sats < 1) throw new Error(what + ": must be at least 0.00000001");
  return sats;
}
const whole = (text, what, min) => {
  const t = String(text).trim();
  if (!/^\d+$/.test(t) || Number(t) < min) throw new Error(what + ": enter a whole number, at least " + min);
  return Number(t);
};

(function () {
  const form = $("newform");
  if (!form) return;
  const sel = (id, options, value) => { const s = h("select"); s.id = id;
    options.forEach(o => s.append(new Option(o, o))); if (value) s.value = value; return s; };
  const field = (label, control) => { const d = h("div", "field"); d.append(h("span", null, label), control); return d; };
  const num = (id, value, width) => { const i = h("input"); i.id = id; i.value = value; i.style.maxWidth = width || "140px"; return i; };
  let built = "", middles = [];

  // Straight path: source -> the wallets you pick, in the order shown -> destination.
  // The first hop sends the amount you enter; every later hop sends that wallet's whole balance minus the fee.
  function path() { return [$("f-src").value, ...middles.filter(m => m.on).map(m => m.name), $("f-dst").value]; }

  function drawMiddles() {
    const box = $("f-mids"); box.replaceChildren();
    middles.forEach((m, i) => {
      const row = h("div", "midrow"), l = h("label"), c = h("input");
      c.type = "checkbox"; c.checked = m.on; c.onchange = () => { m.on = c.checked; drawMiddles(); refresh(); };
      l.append(c, document.createTextNode(m.name));
      const up = h("button", null, "Up"), dn = h("button", null, "Down");
      up.type = dn.type = "button"; up.disabled = i === 0; dn.disabled = i === middles.length - 1;
      up.onclick = () => { [middles[i - 1], middles[i]] = [middles[i], middles[i - 1]]; drawMiddles(); refresh(); };
      dn.onclick = () => { [middles[i + 1], middles[i]] = [middles[i], middles[i + 1]]; drawMiddles(); refresh(); };
      row.append(l, up, dn); box.append(row);
    });
  }

  function others() {
    const src = $("f-src").value, dst = $("f-dst").value, keep = new Map(middles.map(m => [m.name, m.on]));
    middles = (window.flowWallets || []).filter(w => w !== src && w !== dst).map(w => ({name: w, on: keep.has(w) ? keep.get(w) : true}));
    drawMiddles();
  }

  function build() {
    const wallets = window.flowWallets || [];
    if (!wallets.length || built === wallets.join()) return;
    built = wallets.join(); form.replaceChildren();
    const desc = h("input"); desc.id = "f-desc"; desc.placeholder = "what is this run for?";
    const src = sel("f-src", wallets, wallets[0]), dst = sel("f-dst", wallets, wallets[wallets.length - 1]);
    src.onchange = dst.onchange = () => { others(); refresh(); };
    const mids = h("div"); mids.id = "f-mids";
    const route = h("div", "route mono"); route.id = "f-route";
    const wal = h("fieldset"); wal.append(h("legend", null, "Route"), field("Starts at", src), field("Passes through", mids),
      field("Ends at (always)", dst), field("Path", route));

    const amt = h("fieldset"); amt.append(h("legend", null, "Amounts and timing"),
      field("Amount to send from the source (DGB)", num("f-amt", "2")),
      field("Wait between hops (seconds)", num("f-delay", "60", "110px")),
      field("Confirmations required", num("f-conf", "2", "80px")),
      h("div", "mute", "After the first hop, each wallet sends everything it holds minus the fee, so no small leftovers stay behind and the funds end in the destination."));

    const adv = h("details"); adv.id = "f-adv"; adv.append(h("summary", null, "Advanced: list every hop yourself"));
    const rows = h("div"); rows.id = "f-rows"; const add = h("button", null, "Add hop"); add.type = "button";
    add.onclick = () => addRow(rows, wallets);
    const use = h("label"), uc = h("input"); uc.type = "checkbox"; uc.id = "f-useadv"; use.append(uc, document.createTextNode("Use this hop list instead of the route above"));
    adv.append(use, rows, add, h("div", "mute", "Amount is in DGB, or the word all for the wallet's whole balance minus the fee (not allowed out of the source). The last hop must end in the destination; the server refuses anything else."));
    addRow(rows, wallets, wallets[0], wallets[1] || wallets[0], "2");

    const go = h("button", "primary", "Create and review"); go.type = "submit";
    const prev = h("details"); prev.append(h("summary", null, "Config JSON"), h("pre", "mono")); prev.lastChild.id = "f-json";
    form.append(field("Description", desc), wal, amt, adv, go, prev);
    others(); refresh();
  }

  function addRow(rows, wallets, from, to, amount) {
    const r = h("div", "hoprow"), f = h("select"), t = h("select"), a = h("input"), d = h("input"), x = h("button", null, "Remove");
    wallets.forEach(w => { f.append(new Option(w, w)); t.append(new Option(w, w)); });
    f.value = from || wallets[0]; t.value = to || wallets[0]; a.value = amount || "1"; a.placeholder = "DGB or all";
    d.value = "60"; d.placeholder = "delay s"; x.type = "button"; x.onclick = () => r.remove();
    r.append(f, t, a, d, x); rows.append(r);
  }

  function refresh() { const r = $("f-route"); if (r) r.textContent = path().join("  ->  "); }

  function collect() {
    const src = $("f-src").value, dst = $("f-dst").value;
    if (src === dst) throw new Error("Source and destination must be different wallets");
    const delay = whole($("f-delay").value, "Wait between hops", 0), amount = toSats($("f-amt").value, "Amount");
    const flow = {description: $("f-desc").value.trim() || "dashboard run", source_wallet: src,
      flow_wallets: middles.filter(m => m.on).map(m => m.name), destination_wallet: dst, allocation_sats: amount};
    if ($("f-useadv").checked) {
      flow.transfers = [...$("f-rows").children].map((r, i) => { const [f, t, a, d] = r.children, n = "Hop " + (i + 1);
        return {from: f.value, to: t.value, amount_sats: a.value.trim().toLowerCase() === "all" ? "all" : toSats(a.value, n + " amount"),
          delay_seconds: whole(d.value, n + " delay", 0)}; });
      if (!flow.transfers.length) throw new Error("Add at least one hop");
      flow.allocation_sats = Math.max(amount, ...flow.transfers.filter(t => t.from === src && t.amount_sats !== "all").map(t => t.amount_sats));
    } else {
      const p = path();
      flow.transfers = p.slice(0, -1).map((from, i) => ({from, to: p[i + 1], amount_sats: i === 0 ? amount : "all", delay_seconds: delay}));
    }
    return {flows: [flow], confirmations_required: whole($("f-conf").value, "Confirmations", 1),
      fee_policy: {type: "minimum"}, address_policy: "new"};
  }

  form.addEventListener("input", () => { refresh(); try { $("f-json").textContent = JSON.stringify(collect(), null, 2); }
    catch (err) { $("f-json").textContent = String(err.message); } });
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    let cfg; try { cfg = collect(); } catch (err) { toast(err.message, true); return; }
    const out = await act("new", {config: cfg, description: $("f-desc").value.trim()}, "Created. Review it below, then approve.");
    if (!out) return;
    picked = out.exp;
    const box = $("newreview"); buildApprove(box, {exp: out.exp, hash: out.hash, text: out.text},
      () => { box.replaceChildren(); showTab("monitor"); });
  });
  setInterval(build, 1000); build();
})();
