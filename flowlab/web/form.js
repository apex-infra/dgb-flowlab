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
  let built = "";

  function build() {
    const wallets = window.flowWallets || [];
    if (!wallets.length || built === wallets.join()) return;
    built = wallets.join(); form.replaceChildren();
    const desc = h("input"); desc.id = "f-desc"; desc.placeholder = "what is this run for?";
    const src = sel("f-src", wallets, wallets[0]), dst = sel("f-dst", wallets, wallets[wallets.length - 1]);
    const mids = h("div"); mids.id = "f-mids";
    wallets.forEach(w => { const l = h("label"), c = h("input"); c.type = "checkbox"; c.value = w;
      c.checked = w !== wallets[0] && w !== wallets[wallets.length - 1]; l.append(c, document.createTextNode(w)); mids.append(l); });
    const wal = h("fieldset"); wal.append(h("legend", null, "Wallets"), field("Source", src), field("Flow wallets in between", mids), field("Destination", dst),
      field("Allocation from the source (DGB)", num("f-alloc", "3")), field("Confirmations required", num("f-conf", "2", "80px")));

    const mode = h("fieldset"); mode.append(h("legend", null, "Hops"));
    const rb = (v, t, on) => { const l = h("label"), r = h("input"); r.type = "radio"; r.name = "mode"; r.value = v; r.checked = on;
      r.onchange = showMode; l.append(r, document.createTextNode(t)); return l; };
    mode.append(h("div", "row"));
    mode.lastChild.append(rb("repeat", "Repeat around a cycle", true), rb("explicit", "List every hop", false));
    const rep = h("div"); rep.id = "f-repeat";
    const cyc = h("input"); cyc.id = "f-cycle"; cyc.value = [wallets[0], ...wallets.slice(1, -1)].join(", ");
    const kind = sel("f-kind", ["same amount every hop", "step down each hop", "other wallets send their whole balance"]);
    rep.append(field("Cycle (starts at the source)", cyc), field("Number of hops", num("f-count", "3", "90px")),
      field("Amount per hop (DGB)", num("f-amt", "2")), field("Delay between hops (seconds)", num("f-delay", "60", "110px")),
      field("Amounts", kind), field("Step down (DGB)", num("f-step", "0.02")));
    const exp = h("div"); exp.id = "f-explicit"; exp.hidden = true;
    const rows = h("div"); rows.id = "f-rows"; const add = h("button", null, "Add hop"); add.type = "button";
    add.onclick = () => addRow(rows, wallets);
    exp.append(rows, add, h("div", "mute", "Amount is in DGB, or the word all for the wallet's whole balance minus the fee. Not allowed out of the source."));
    addRow(rows, wallets, wallets[0], wallets[1] || wallets[0], "2");
    mode.append(rep, exp);
    kind.onchange = () => { $("f-step").parentNode.hidden = kind.value !== "step down each hop"; };

    const go = h("button", "primary", "Create and review"); go.type = "submit";
    const prev = h("details"); prev.append(h("summary", null, "Config JSON"), h("pre", "mono")); prev.lastChild.id = "f-json";
    form.append(field("Description", desc), wal, mode, go, prev);
    kind.onchange();
  }

  function addRow(rows, wallets, from, to, amount) {
    const r = h("div", "hoprow"), f = h("select"), t = h("select"), a = h("input"), d = h("input"), x = h("button", null, "Remove");
    wallets.forEach(w => { f.append(new Option(w, w)); t.append(new Option(w, w)); });
    f.value = from || wallets[0]; t.value = to || wallets[0]; a.value = amount || "1"; a.placeholder = "DGB or all";
    d.value = "60"; d.placeholder = "delay s"; x.type = "button"; x.onclick = () => r.remove();
    r.append(f, t, a, d, x); rows.append(r);
  }

  function showMode() {
    const m = form.querySelector("input[name=mode]:checked").value;
    $("f-repeat").hidden = m !== "repeat"; $("f-explicit").hidden = m !== "explicit";
  }

  function collect() {
    const src = $("f-src").value, dst = $("f-dst").value;
    const mids = [...$("f-mids").querySelectorAll("input:checked")].map(c => c.value);
    const flow = {description: $("f-desc").value.trim() || "dashboard run", source_wallet: src, flow_wallets: mids,
      destination_wallet: dst, allocation_sats: toSats($("f-alloc").value, "Allocation")};
    if (form.querySelector("input[name=mode]:checked").value === "repeat") {
      const kind = $("f-kind").value;
      const rep = {cycle: $("f-cycle").value.split(",").map(s => s.trim()).filter(Boolean), count: whole($("f-count").value, "Number of hops", 1),
        amount_sats: toSats($("f-amt").value, "Amount"), delay_seconds: whole($("f-delay").value, "Delay", 0)};
      if (kind === "step down each hop") rep.step_down_sats = toSats($("f-step").value, "Step down");
      if (kind.startsWith("other wallets")) rep.sweep = true;
      flow.repeat = rep;
    } else {
      flow.transfers = [...$("f-rows").children].map((r, i) => { const [f, t, a, d] = r.children, n = "Hop " + (i + 1);
        return {from: f.value, to: t.value, amount_sats: a.value.trim().toLowerCase() === "all" ? "all" : toSats(a.value, n + " amount"),
          delay_seconds: whole(d.value, n + " delay", 0)}; });
      if (!flow.transfers.length) throw new Error("Add at least one hop");
    }
    return {flows: [flow], confirmations_required: whole($("f-conf").value, "Confirmations", 1),
      fee_policy: {type: "minimum"}, address_policy: "new"};
  }

  form.addEventListener("input", () => { try { $("f-json").textContent = JSON.stringify(collect(), null, 2); }
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
