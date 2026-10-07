"use strict";

/*
 * New experiment builder.
 *
 * Deterministic mode builds the existing explicit transfer path.
 * Experimental mode builds a seeded, approved workload and topology.
 * The server remains authoritative and validates the completed config.
 */

function toSats(text, what) {
  const t = String(text).trim();
  if (!/^\d+(\.\d{1,8})?$/.test(t))
    throw new Error(what + ": enter a DGB amount such as 2 or 0.5 (up to 8 decimals)");
  const [i, f = ""] = t.split(".");
  const sats = Number(i) * 1e8 + Number((f + "00000000").slice(0, 8));
  if (!Number.isSafeInteger(sats) || sats < 1)
    throw new Error(what + ": must be at least 0.00000001");
  return sats;
}

const whole = (text, what, min) => {
  const t = String(text).trim();
  if (!/^\d+$/.test(t) || Number(t) < min)
    throw new Error(what + ": enter a whole number, at least " + min);
  return Number(t);
};

(function () {
  const form = $("newform");
  if (!form) return;

  const state = {
    builtFor: "",
    mode: "experimental",
    workers: [],
    edges: new Set(),
    playConfig: null,
    previewSeq: 0
  };

  const sel = (id, options, value) => {
    const s = h("select");
    s.id = id;
    options.forEach(o => s.append(new Option(o, o)));
    if (value) s.value = value;
    return s;
  };

  const field = (label, control) => {
    const d = h("div", "field");
    d.append(h("span", null, label), control);
    return d;
  };

  const num = (id, value, width) => {
    const i = h("input");
    i.id = id;
    i.value = value;
    i.inputMode = "decimal";
    i.style.maxWidth = width || "150px";
    return i;
  };

  const note = text => h("div", "mute form-note", text);

  function newSeed() {
    const words = new Uint32Array(1);
    crypto.getRandomValues(words);
    return String(words[0]);
  }

  function edgeKey(from, to) {
    return from + "\u0000" + to;
  }

  function wallets() {
    return window.flowWallets || [];
  }

  function walletRoles() {
    return window.flowWalletRoles || {};
  }

  function roleWallets(name) {
    const values = walletRoles()[name];
    return Array.isArray(values) && values.length ? values : wallets();
  }

  function workloadWallets() {
    const roles = walletRoles();
    const values = [
      ...(Array.isArray(roles.workers) ? roles.workers : []),
      ...(Array.isArray(roles.hubs) ? roles.hubs : [])
    ];
    return values.length ? [...new Set(values)] : wallets();
  }

  function plays() {
    return window.flowPlays || [];
  }

  function modeButtons() {
    const box = h("div", "mode-switch");
    const det = h("button", state.mode === "deterministic" ? "on" : null, "Deterministic");
    const exp = h("button", state.mode === "experimental" ? "on" : null, "Experimental");
    const play = h("button", state.mode === "play" ? "on" : null, "Play");
    det.type = exp.type = play.type = "button";

    det.onclick = () => {
      state.mode = "deterministic";
      build(true);
    };
    exp.onclick = () => {
      state.mode = "experimental";
      build(true);
    };
    play.onclick = () => {
      state.mode = "play";
      build(true);
    };

    box.append(det, exp, play);
    return box;
  }

  // ------------------------------------------------ deterministic

  function deterministicPath() {
    return [
      $("f-src").value,
      ...state.workers.filter(x => x.on).map(x => x.name),
      $("f-dst").value
    ];
  }

  function drawDeterministicWorkers() {
    const box = $("f-mids");
    if (!box) return;
    box.replaceChildren();

    state.workers.forEach((m, i) => {
      const row = h("div", "midrow");
      const label = h("label");
      const check = h("input");

      check.type = "checkbox";
      check.checked = m.on;
      check.onchange = () => {
        m.on = check.checked;
        drawDeterministicWorkers();
        refreshPreview();
      };

      label.append(check, document.createTextNode(m.name));

      const up = h("button", null, "Up");
      const down = h("button", null, "Down");
      up.type = down.type = "button";
      up.disabled = i === 0;
      down.disabled = i === state.workers.length - 1;

      up.onclick = () => {
        [state.workers[i - 1], state.workers[i]] =
          [state.workers[i], state.workers[i - 1]];
        drawDeterministicWorkers();
        refreshPreview();
      };

      down.onclick = () => {
        [state.workers[i + 1], state.workers[i]] =
          [state.workers[i], state.workers[i + 1]];
        drawDeterministicWorkers();
        refreshPreview();
      };

      row.append(label, up, down);
      box.append(row);
    });
  }

  function refreshDeterministicWorkers() {
    const src = $("f-src").value;
    const dst = $("f-dst").value;
    const keep = new Map(state.workers.map(x => [x.name, x.on]));

    state.workers = workloadWallets()
      .filter(w => w !== src && w !== dst)
      .map(w => ({
        name: w,
        on: keep.has(w) ? keep.get(w) : true
      }));

    drawDeterministicWorkers();
  }

  function addHopRow(rows, ws, from, to, amount) {
    const row = h("div", "hoprow");
    const f = h("select");
    const t = h("select");
    const a = h("input");
    const d = h("input");
    const remove = h("button", null, "Remove");

    ws.forEach(w => {
      f.append(new Option(w, w));
      t.append(new Option(w, w));
    });

    f.value = from || ws[0];
    t.value = to || ws[0];
    a.value = amount || "1";
    a.placeholder = "DGB or all";
    d.value = "60";
    d.placeholder = "delay s";

    remove.type = "button";
    remove.onclick = () => {
      row.remove();
      refreshPreview();
    };

    for (const c of [f, t, a, d]) c.oninput = refreshPreview;

    row.append(f, t, a, d, remove);
    rows.append(row);
  }

  function buildDeterministic(body, ws) {
    const reserves = roleWallets("reserve");
    const destinations = roleWallets("destinations");
    const workload = workloadWallets();

    const src = sel("f-src", reserves, reserves[0]);
    const dst = sel("f-dst", destinations, destinations[0]);

    const deterministicWallets = [
      ...new Set([...reserves, ...workload, ...destinations])
    ];

    src.onchange = dst.onchange = () => {
      refreshDeterministicWorkers();
      refreshPreview();
    };

    const mids = h("div");
    mids.id = "f-mids";

    const route = h("div", "route mono");
    route.id = "f-route";

    const wal = h("fieldset");
    wal.append(
      h("legend", null, "Route"),
      field("Reserve / funding wallet", src),
      field("Passes through workers / hubs", mids),
      field("Final destination", dst),
      field("Path", route),
      note("Deterministic routes use reserve → workers/hubs → destination. Stage wallets are reserved for Experimental and Play allocation.")
    );

    const amt = h("fieldset");
    amt.append(
      h("legend", null, "Amounts and timing"),
      field("Amount from source (DGB)", num("f-amt", "2")),
      field("Wait between hops (seconds)", num("f-delay", "60", "110px")),
      field("Confirmations required", num("f-conf", "2", "80px")),
      note("After the first hop, later wallets send their whole available balance minus the transaction fee.")
    );

    const adv = h("details");
    adv.id = "f-adv";
    adv.append(h("summary", null, "Advanced: list every hop yourself"));

    const rows = h("div");
    rows.id = "f-rows";

    const add = h("button", null, "Add hop");
    add.type = "button";
    add.onclick = () => {
      addHopRow(rows, deterministicWallets);
      refreshPreview();
    };

    const use = h("label");
    const check = h("input");
    check.type = "checkbox";
    check.id = "f-useadv";
    check.onchange = refreshPreview;
    use.append(check, document.createTextNode("Use this hop list instead of the route above"));

    adv.append(
      use,
      rows,
      add,
      note("The final explicit hop must end in the destination wallet.")
    );

    addHopRow(
      rows,
      deterministicWallets,
      src.value,
      workload[0] || dst.value,
      "2"
    );

    body.append(wal, amt, adv);
    refreshDeterministicWorkers();
  }

  function collectDeterministic() {
    const src = $("f-src").value;
    const dst = $("f-dst").value;

    if (src === dst)
      throw new Error("Source and destination must be different wallets");

    const amount = toSats($("f-amt").value, "Amount");
    const delay = whole($("f-delay").value, "Wait between hops", 0);

    const flow = {
      description: $("f-desc").value.trim() || "dashboard run",
      source_wallet: src,
      flow_wallets: state.workers.filter(x => x.on).map(x => x.name),
      destination_wallet: dst,
      allocation_sats: amount
    };

    if ($("f-useadv").checked) {
      flow.transfers = [...$("f-rows").children].map((row, i) => {
        const [f, t, a, d] = row.children;
        const name = "Hop " + (i + 1);

        return {
          from: f.value,
          to: t.value,
          amount_sats:
            a.value.trim().toLowerCase() === "all"
              ? "all"
              : toSats(a.value, name + " amount"),
          delay_seconds: whole(d.value, name + " delay", 0)
        };
      });

      if (!flow.transfers.length)
        throw new Error("Add at least one hop");

      flow.allocation_sats = Math.max(
        amount,
        ...flow.transfers
          .filter(x => x.from === src && x.amount_sats !== "all")
          .map(x => x.amount_sats)
      );
    } else {
      const p = deterministicPath();
      flow.transfers = p.slice(0, -1).map((from, i) => ({
        from,
        to: p[i + 1],
        amount_sats: i === 0 ? amount : "all",
        delay_seconds: delay
      }));
    }

    return {
      flows: [flow],
      confirmations_required: whole($("f-conf").value, "Confirmations", 1),
      fee_policy: {type: "minimum"},
      address_policy: "new"
    };
  }

  // ------------------------------------------------ experimental

  function experimentalWorkers() {
    const src = $("f-src").value;
    const stage = $("f-stage").value;
    const dst = $("f-dst").value;
    return workloadWallets().filter(w => w !== src && w !== stage && w !== dst);
  }

  function resetDefaultEdges() {
    const stage = $("f-stage").value;
    const workers = experimentalWorkers();

    state.edges = new Set();

    for (const to of workers)
      state.edges.add(edgeKey(stage, to));

    for (const from of workers)
      for (const to of workers)
        state.edges.add(edgeKey(from, to));
  }

  function drawTopology() {
    const box = $("f-topology");
    if (!box) return;

    box.replaceChildren();

    const stage = $("f-stage").value;
    const workers = experimentalWorkers();

    if (!workers.length) {
      box.append(note("Choose at least one working wallet in addition to the allocation wallet."));
      return;
    }

    const table = h("div", "topology-grid");
    table.style.gridTemplateColumns = `repeat(${workers.length + 1}, minmax(120px, 1fr))`;

    table.append(h("div", "topology-corner", "FROM / TO"));
    workers.forEach(to => table.append(h("div", "topology-head mono", to)));

    const froms = [stage, ...workers];

    froms.forEach(from => {
      table.append(h("div", "topology-head mono", from));

      workers.forEach(to => {
        const cell = h("label", "topology-cell");
        const check = h("input");
        const key = edgeKey(from, to);

        check.type = "checkbox";

        check.checked = state.edges.has(key);

        check.onchange = () => {
          if (check.checked) state.edges.add(key);
          else state.edges.delete(key);
          refreshPreview();
        };

        cell.append(check, document.createTextNode(from === to ? "self" : "allow"));
        table.append(cell);
      });
    });

    box.append(table);
  }

  function experimentalWalletChanged() {
    resetDefaultEdges();
    drawTopology();
    refreshPreview();
  }

  function buildExperimental(body, ws) {
    const reserves = roleWallets("reserve");
    const stages = roleWallets("stage");
    const destinations = roleWallets("destinations");

    const src = sel("f-src", reserves, reserves[0]);
    const stage = sel("f-stage", stages, stages[0]);
    const dst = sel("f-dst", destinations, destinations[0]);

    src.onchange = stage.onchange = dst.onchange = experimentalWalletChanged;

    const topology = h("div");
    topology.id = "f-topology";

    const walletsBox = h("fieldset");
    walletsBox.append(
      h("legend", null, "Wallet roles"),
      field("Reserve / funding wallet", src),
      field("Allocation wallet", stage),
      field("Final destination", dst),
      note("The reserve commits the full allocation to the allocation wallet before randomized workload begins. The reserve and destination are outside randomized routing.")
    );

    const workload = h("fieldset");
    workload.append(
      h("legend", null, "Experimental workload"),
      field("Experiment allocation (DGB)", num("f-allocation", "2")),
      field("Number of decisions", num("f-jobs", "20", "100px")),
      field("Minimum amount (DGB)", num("f-minamt", "0.10")),
      field("Maximum amount (DGB)", num("f-maxamt", "1.00")),
      field("Minimum delay (seconds)", num("f-mindelay", "5", "100px")),
      field("Maximum delay (seconds)", num("f-maxdelay", "60", "100px")),
      field("Confirmations required", num("f-conf", "2", "80px"))
    );

    const random = h("fieldset");
    const model = sel(
      "f-model",
      ["uniform", "seeded_deterministic"],
      "uniform"
    );

    const seed = num("f-seed", newSeed(), "180px");
    const seedButton = h("button", null, "New seed");
    seedButton.type = "button";
    seedButton.onclick = () => {
      seed.value = newSeed();
      refreshPreview();
    };

    const seedRow = h("div", "seed-row");
    seedRow.append(seed, seedButton);

    random.append(
      h("legend", null, "Randomization"),
      field("Model", model),
      field("Seed", seedRow),
      note("A fresh seed is generated for each new experimental form. The seed remains editable and becomes part of the approved configuration so the run can be reproduced.")
    );

    const topo = h("fieldset");
    topo.append(
      h("legend", null, "Approved topology"),
      topology,
      note("The allocation wallet is the workload entry point. Diagonal worker cells are self-transfers. The reserve and destination are excluded from experimental routing.")
    );

    body.append(walletsBox, workload, random, topo);

    resetDefaultEdges();
    drawTopology();
  }

  function collectExperimental() {
    const src = $("f-src").value;
    const stage = $("f-stage").value;
    const dst = $("f-dst").value;
    const workers = experimentalWorkers();

    if (new Set([src, stage, dst]).size !== 3)
      throw new Error("Reserve, allocation wallet, and destination must be different wallets");

    if (!workers.length)
      throw new Error("Experimental mode needs at least one working wallet");

    const allocation = toSats($("f-allocation").value, "Experiment allocation");
    const minAmount = toSats($("f-minamt").value, "Minimum amount");
    const maxAmount = toSats($("f-maxamt").value, "Maximum amount");

    if (maxAmount < minAmount)
      throw new Error("Maximum amount must be at least the minimum amount");

    if (maxAmount > allocation)
      throw new Error("Maximum amount cannot exceed the experiment allocation");

    const minDelay = whole($("f-mindelay").value, "Minimum delay", 0);
    const maxDelay = whole($("f-maxdelay").value, "Maximum delay", 0);

    if (maxDelay < minDelay)
      throw new Error("Maximum delay must be at least the minimum delay");

    const transitions = [];

    for (const from of [stage, ...workers]) {
      for (const to of workers) {
        if (state.edges.has(edgeKey(from, to)))
          transitions.push({from, to});
      }
    }

    if (!transitions.some(x => x.from === stage))
      throw new Error("Allow at least one transition out of the allocation wallet");

    const seed = whole($("f-seed").value, "Seed", 0);

    return {
      flows: [{
        description: $("f-desc").value.trim() || "experimental dashboard run",
        source_wallet: src,
        allocation_wallet: stage,
        flow_wallets: [stage, ...workers],
        destination_wallet: dst,
        allocation_sats: allocation,
        experimental_topology: {transitions}
      }],
      workload: {
        mode: "count",
        jobs: whole($("f-jobs").value, "Number of decisions", 1),
        amount_sats_min: minAmount,
        amount_sats_max: maxAmount,
        delay_seconds_min: minDelay,
        delay_seconds_max: maxDelay
      },
      confirmations_required: whole($("f-conf").value, "Confirmations", 1),
      fee_policy: {type: "minimum"},
      address_policy: "new",
      randomization: {
        enabled: true,
        model: $("f-model").value,
        seed
      },
      finalization: {
        mode: "sweep_workers_to_destination"
      }
    };
  }

  // ------------------------------------------------ play

  function selectedPlayWorkers() {
    return state.workers.filter(x => x.on).map(x => x.name);
  }

  function refreshPlayWorkers() {
    const src = $("f-src").value;
    const stage = $("f-stage").value;
    const dst = $("f-dst").value;
    const keep = new Map(state.workers.map(x => [x.name, x.on]));

    state.workers = workloadWallets()
      .filter(w => w !== src && w !== stage && w !== dst)
      .map(w => ({
        name: w,
        on: keep.has(w) ? keep.get(w) : true
      }));

    drawPlayWorkers();
  }

  function drawPlayWorkers() {
    const box = $("f-play-workers");
    if (!box) return;

    box.replaceChildren();

    state.workers.forEach(w => {
      const label = h("label");
      const check = h("input");

      check.type = "checkbox";
      check.checked = w.on;
      check.onchange = () => {
        w.on = check.checked;
        refreshPreview();
      };

      label.append(check, document.createTextNode(w.name));
      box.append(label);
    });
  }

  function playParams() {
    const workers = selectedPlayWorkers();

    if (!workers.length)
      throw new Error("Play mode needs at least one working wallet");

    const allocation = toSats($("f-allocation").value, "Experiment allocation");
    const minAmount = toSats($("f-minamt").value, "Minimum amount");
    const maxAmount = toSats($("f-maxamt").value, "Maximum amount");

    if (maxAmount < minAmount)
      throw new Error("Maximum amount must be at least the minimum amount");

    if (maxAmount > allocation)
      throw new Error("Maximum amount cannot exceed the experiment allocation");

    const minDelay = whole($("f-mindelay").value, "Minimum delay", 0);
    const maxDelay = whole($("f-maxdelay").value, "Maximum delay", 0);

    if (maxDelay < minDelay)
      throw new Error("Maximum delay must be at least the minimum delay");

    const src = $("f-src").value;
    const stage = $("f-stage").value;
    const dst = $("f-dst").value;

    if (new Set([src, stage, dst]).size !== 3)
      throw new Error("Reserve, allocation wallet, and destination must be different wallets");

    return {
      source_wallet: src,
      allocation_wallet: stage,
      workers,
      destination_wallet: dst,
      allocation_sats: allocation,
      decisions: whole($("f-jobs").value, "Number of decisions", 1),
      amount_sats_min: minAmount,
      amount_sats_max: maxAmount,
      delay_seconds_min: minDelay,
      delay_seconds_max: maxDelay,
      confirmations_required: whole($("f-conf").value, "Confirmations", 1),
      seed: whole($("f-seed").value, "Seed", 0)
    };
  }

  function playRequest() {
    return {
      play: $("f-play").value,
      params: playParams()
    };
  }

  function buildPlay(body, ws) {
    const catalog = plays();

    if (!catalog.length) {
      body.append(note("No plays are available from the server."));
      return;
    }

    const play = h("select");
    play.id = "f-play";
    catalog.forEach(p => play.append(new Option(p.title, p.name)));

    const reserves = roleWallets("reserve");
    const stages = roleWallets("stage");
    const destinations = roleWallets("destinations");

    const src = sel("f-src", reserves, reserves[0]);
    const stage = sel("f-stage", stages, stages[0]);
    const dst = sel("f-dst", destinations, destinations[0]);

    const workerBox = h("div", "row");
    workerBox.id = "f-play-workers";

    const roles = h("fieldset");
    roles.append(
      h("legend", null, "Play"),
      field("Strategy", play),
      field("Reserve / funding wallet", src),
      field("Allocation wallet", stage),
      field("Working wallets", workerBox),
      field("Final destination", dst),
      note("Play topology is compiled by FlowLab on the server. The resulting ordinary config is still reviewed, hashed, and approved before execution.")
    );

    const workload = h("fieldset");
    workload.append(
      h("legend", null, "Play parameters"),
      field("Experiment allocation (DGB)", num("f-allocation", "2")),
      field("Number of decisions", num("f-jobs", "20", "100px")),
      field("Minimum amount (DGB)", num("f-minamt", "0.10")),
      field("Maximum amount (DGB)", num("f-maxamt", "1.00")),
      field("Minimum delay (seconds)", num("f-mindelay", "5", "100px")),
      field("Maximum delay (seconds)", num("f-maxdelay", "60", "100px")),
      field("Confirmations required", num("f-conf", "2", "80px"))
    );

    const seed = num("f-seed", newSeed(), "180px");
    const seedButton = h("button", null, "New seed");
    seedButton.type = "button";
    seedButton.onclick = () => {
      seed.value = newSeed();
      refreshPreview();
    };

    const seedRow = h("div", "seed-row");
    seedRow.append(seed, seedButton);

    const replay = h("fieldset");
    replay.append(
      h("legend", null, "Reproducibility"),
      field("Seed", seedRow),
      note("The seed becomes part of the compiled and approved config.")
    );

    body.append(roles, workload, replay);

    src.onchange = stage.onchange = dst.onchange = () => {
      refreshPlayWorkers();
      refreshPreview();
    };

    play.onchange = refreshPreview;

    refreshPlayWorkers();
  }

  async function compilePlayForPreview() {
    const seq = ++state.previewSeq;
    state.playConfig = null;

    const out = $("f-json");
    if (!out) return;

    let req;
    try {
      req = playRequest();
    } catch (err) {
      out.textContent = String(err.message);
      return;
    }

    out.textContent = "Compiling play...";

    try {
      const compiled = await api("compile_play", req);
      if (seq !== state.previewSeq) return;

      state.playConfig = compiled.config;
      out.textContent = JSON.stringify(compiled.config, null, 2);
    } catch (err) {
      if (seq !== state.previewSeq) return;
      out.textContent = String(err.message);
    }
  }

  // ------------------------------------------------ common

  function collect() {
    if (state.mode === "experimental")
      return collectExperimental();

    if (state.mode === "play") {
      if (!state.playConfig)
        throw new Error("Compile the play successfully before creating it");
      return state.playConfig;
    }

    return collectDeterministic();
  }

  function refreshPreview() {
    if (state.mode === "play") {
      compilePlayForPreview();
      return;
    }

    const route = $("f-route");
    if (route && state.mode === "deterministic")
      route.textContent = deterministicPath().join("  ->  ");

    const out = $("f-json");
    if (!out) return;

    try {
      out.textContent = JSON.stringify(collect(), null, 2);
    } catch (err) {
      out.textContent = String(err.message);
    }
  }

  function build(force) {
    const ws = wallets();
    const signature = ws.join("\u0001") + "|" + state.mode;

    if (!ws.length || (!force && state.builtFor === signature))
      return;

    state.builtFor = signature;
    state.workers = [];
    state.edges = new Set();
    state.playConfig = null;
    state.previewSeq += 1;
    form.replaceChildren();

    const desc = h("input");
    desc.id = "f-desc";
    desc.placeholder = "what is this run for?";

    const body = h("div");
    body.id = "f-mode-body";

    form.append(
      field("Experiment type", modeButtons()),
      field("Description", desc),
      body
    );

    if (state.mode === "experimental")
      buildExperimental(body, ws);
    else if (state.mode === "play")
      buildPlay(body, ws);
    else
      buildDeterministic(body, ws);

    const go = h("button", "primary", "Create and review");
    go.type = "submit";

    const preview = h("details");
    preview.append(
      h("summary", null, "Config JSON"),
      h("pre", "mono")
    );
    preview.lastChild.id = "f-json";

    form.append(go, preview);

    form.querySelectorAll("input,select").forEach(el => {
      if (!el.oninput) el.addEventListener("input", refreshPreview);
      el.addEventListener("change", refreshPreview);
    });

    refreshPreview();
  }

  form.addEventListener("submit", async ev => {
    ev.preventDefault();

    let cfg;
    try {
      if (state.mode === "play") {
        const compiled = await api("compile_play", playRequest());
        cfg = compiled.config;
        state.playConfig = cfg;
      } else {
        cfg = collect();
      }
    } catch (err) {
      toast(err.message, true);
      return;
    }

    const out = await act(
      "new",
      {
        config: cfg,
        description: $("f-desc").value.trim()
      },
      "Created. Review it below, then approve."
    );

    if (!out) return;

    picked = out.exp;

    const box = $("newreview");
    buildApprove(
      box,
      {exp: out.exp, hash: out.hash, text: out.text},
      () => {
        box.replaceChildren();
        showTab("monitor");
      }
    );
  });

  setInterval(() => build(false), 1000);
  build(true);
})();
