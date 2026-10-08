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
    previewSeq: 0,
    destinationRows: null
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

  function destinationEndpoint() {
    const kind = $("f-dst-kind");

    if (kind && kind.value === "external") {
      const input = $("f-dst-address");
      const address = input ? input.value.trim() : "";

      if (!address)
        throw new Error("Enter a custom DigiByte destination address");

      return {
        kind: "external",
        key: "destination_address",
        value: address
      };
    }

    const wallet = $("f-dst");

    if (!wallet || !wallet.value)
      throw new Error("Choose a FlowLab destination wallet");

    return {
      kind: "internal",
      key: "destination_wallet",
      value: wallet.value
    };
  }

  function destinationConfig() {
    const endpoint = destinationEndpoint();
    return {[endpoint.key]: endpoint.value};
  }

  function destinationControls(destinations, changed) {
    const kind = h("select");
    kind.id = "f-dst-kind";
    kind.append(
      new Option("FlowLab wallet", "internal"),
      new Option("Custom DigiByte address", "external")
    );

    const wallet = sel("f-dst", destinations, destinations[0]);

    const address = h("input");
    address.id = "f-dst-address";
    address.placeholder = "DigiByte address";
    address.autocomplete = "off";
    address.spellcheck = false;

    const kindField = field("Destination type", kind);
    const walletField = field("Final destination wallet", wallet);
    const addressField = field("Custom DigiByte address", address);

    function sync(notify = true) {
      const external = kind.value === "external";
      walletField.hidden = external;
      addressField.hidden = !external;

      if (notify && changed)
        changed();
    }

    kind.onchange = () => sync(true);
    wallet.onchange = () => changed && changed();
    address.oninput = () => changed && changed();

    sync(false);

    return {
      fields: [kindField, walletField, addressField],
      sync
    };
  }

  function percentToBps(text, what) {
    const t = String(text).trim();

    if (!/^\d+(\.\d{1,2})?$/.test(t))
      throw new Error(
        what + ": enter a percentage with at most 2 decimals"
      );

    const [wholePart, fraction = ""] = t.split(".");
    const bps =
      Number(wholePart) * 100
      + Number((fraction + "00").slice(0, 2));

    if (!Number.isSafeInteger(bps) || bps < 1 || bps > 10000)
      throw new Error(
        what + ": percentage must be greater than 0 and at most 100"
      );

    return bps;
  }

  function formatBps(bps) {
    const wholePart = Math.floor(bps / 100);
    const fraction = String(bps % 100).padStart(2, "0");
    return wholePart + "." + fraction;
  }

  function destinationSetRows() {
    const box = $("f-dst-rows") || state.destinationRows;
    return box ? [...box.children] : [];
  }

  function destinationSetInternalWallets() {
    return destinationSetRows()
      .filter(row => row._kind && row._kind.value === "internal")
      .map(row => row._wallet ? row._wallet.value : "")
      .filter(Boolean);
  }

  function destinationSetConfig() {
    const modeControl = $("f-dst-mode");

    if (!modeControl)
      throw new Error("Destination editor is not available");

    const mode = modeControl.value;
    const rows = destinationSetRows();

    if (!rows.length || rows.length > 10)
      throw new Error("Choose between 1 and 10 terminal destinations");

    const items = [];
    const targets = new Set();
    let percentTotal = 0;
    let remainderCount = 0;

    rows.forEach((row, index) => {
      const n = index + 1;
      const kind = row._kind.value;
      let targetKey;
      let item;

      if (kind === "internal") {
        const wallet = row._wallet.value;

        if (!wallet)
          throw new Error(
            "Destination " + n + ": choose a FlowLab destination wallet"
          );

        targetKey = "wallet:" + wallet;
        item = {
          type: "wallet",
          wallet
        };
      } else {
        const address = row._address.value.trim();

        if (!address)
          throw new Error(
            "Destination " + n + ": enter a DigiByte address"
          );

        targetKey = "address:" + address;
        item = {
          type: "address",
          address
        };
      }

      if (targets.has(targetKey))
        throw new Error(
          "Destination " + n + ": duplicate terminal target"
        );

      targets.add(targetKey);

      if (mode === "percentage") {
        const bps = percentToBps(
          row._percent.value,
          "Destination " + n
        );

        item.percent_bps = bps;
        percentTotal += bps;
      } else if (row._remainder.checked) {
        item.remainder = true;
        remainderCount += 1;
      } else {
        item.amount_sats = toSats(
          row._fixed.value,
          "Destination " + n + " fixed amount"
        );
      }

      items.push(item);
    });

    if (mode === "percentage" && percentTotal !== 10000)
      throw new Error(
        "Destination percentages must total exactly 100.00%"
      );

    if (mode === "fixed" && remainderCount !== 1)
      throw new Error(
        "Fixed distribution requires exactly one remainder destination"
      );

    return {
      mode,
      items
    };
  }

  function destinationSetControls(destinations, changed) {
    const editor = h("div", "destination-editor");

    const toolbar = h("div", "destination-toolbar");

    const mode = h("select");
    mode.id = "f-dst-mode";
    mode.append(
      new Option("Percentage split", "percentage"),
      new Option("Fixed DGB + remainder", "fixed")
    );

    const add = h("button", null, "+ Add destination");
    add.type = "button";
    add.id = "f-dst-add";

    toolbar.append(
      h("span", "mute", "Distribution"),
      mode,
      add
    );

    const rows = h("div", "destination-rows");
    rows.id = "f-dst-rows";
    state.destinationRows = rows;

    const help = note(
      "Choose 1–10 terminal destinations. Each row may be a FlowLab "
      + "destination wallet or a custom DigiByte address. Percentage "
      + "splits must total exactly 100.00%. Fixed mode requires exactly "
      + "one remainder row, which receives everything left after fixed "
      + "amounts and the final transaction fee."
    );

    editor.append(toolbar, rows, help);

    function notify(structural = false) {
      if (changed)
        changed(structural);
    }

    function rebalancePercentRows() {
      const current = destinationSetRows();

      if (!current.length)
        return;

      const base = Math.floor(10000 / current.length);
      let extra = 10000 - base * current.length;

      current.forEach(row => {
        const bps = base + (extra > 0 ? 1 : 0);

        if (extra > 0)
          extra -= 1;

        row._percent.value = formatBps(bps);
      });
    }

    function ensureRemainder() {
      const current = destinationSetRows();

      if (
        current.length
        && !current.some(row => row._remainder.checked)
      )
        current[0]._remainder.checked = true;
    }

    function updateButtons() {
      const current = destinationSetRows();

      add.disabled = current.length >= 10;

      current.forEach(row => {
        row._remove.disabled = current.length <= 1;
      });
    }

    function syncRow(row) {
      const external = row._kind.value === "external";
      const percentage = mode.value === "percentage";

      row._wallet.hidden = external;
      row._address.hidden = !external;

      row._percent.hidden = !percentage;
      row._fixed.hidden = percentage || row._remainder.checked;
      row._remainderLabel.hidden = percentage;

      if (!percentage && row._remainder.checked)
        row._fixed.hidden = true;
    }

    function syncAll() {
      destinationSetRows().forEach(syncRow);
      updateButtons();
    }

    function addRow(initial = {}) {
      if (destinationSetRows().length >= 10)
        return;

      const row = h("div", "destination-row");

      const kind = h("select");
      kind.append(
        new Option("FlowLab wallet", "internal"),
        new Option("Custom DigiByte address", "external")
      );
      kind.value = initial.type === "address"
        ? "external"
        : "internal";

      const wallet = sel(
        "",
        destinations,
        initial.wallet || destinations[0]
      );
      wallet.removeAttribute("id");

      const address = h("input");
      address.placeholder = "DigiByte address";
      address.autocomplete = "off";
      address.spellcheck = false;
      address.value = initial.address || "";

      const percent = h("input");
      percent.inputMode = "decimal";
      percent.placeholder = "%";
      percent.value = initial.percent || "100.00";

      const fixed = h("input");
      fixed.inputMode = "decimal";
      fixed.placeholder = "DGB";
      fixed.value = initial.fixed || "1";

      const remainder = h("input");
      remainder.type = "checkbox";
      remainder.checked = Boolean(initial.remainder);

      const remainderLabel = h("label", "destination-remainder");
      remainderLabel.append(
        remainder,
        document.createTextNode("Remainder")
      );

      const remove = h("button", null, "Remove");
      remove.type = "button";

      row._kind = kind;
      row._wallet = wallet;
      row._address = address;
      row._percent = percent;
      row._fixed = fixed;
      row._remainder = remainder;
      row._remainderLabel = remainderLabel;
      row._remove = remove;

      kind.onchange = () => {
        syncRow(row);
        notify(true);
      };

      wallet.onchange = () => notify(true);
      address.oninput = () => notify(false);
      percent.oninput = () => notify(false);
      fixed.oninput = () => notify(false);

      remainder.onchange = () => {
        if (remainder.checked) {
          destinationSetRows().forEach(other => {
            if (other !== row)
              other._remainder.checked = false;
          });
        }

        ensureRemainder();
        syncAll();
        notify(false);
      };

      remove.onclick = () => {
        row.remove();

        if (mode.value === "percentage")
          rebalancePercentRows();
        else
          ensureRemainder();

        syncAll();
        notify(true);
      };

      row.append(
        kind,
        wallet,
        address,
        percent,
        fixed,
        remainderLabel,
        remove
      );

      rows.append(row);

      if (mode.value === "percentage")
        rebalancePercentRows();
      else
        ensureRemainder();

      syncAll();
    }

    add.onclick = () => {
      addRow({
        type: "wallet",
        fixed: "1"
      });
      notify(true);
    };

    mode.onchange = () => {
      if (mode.value === "percentage")
        rebalancePercentRows();
      else
        ensureRemainder();

      syncAll();
      notify(false);
    };

    addRow({
      type: "wallet",
      wallet: destinations[0],
      percent: "100.00",
      fixed: "1",
      remainder: true
    });

    syncAll();

    return {
      field: field("Terminal destinations", editor),
      sync: syncAll
    };
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
      destinationEndpoint().value
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
    const dst = destinationEndpoint().value;
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

    const destination = destinationControls(destinations, () => {
      refreshDeterministicWorkers();
      refreshPreview();
    });

    const deterministicWallets = [
      ...new Set([...reserves, ...workload, ...destinations])
    ];

    src.onchange = () => {
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
      ...destination.fields,
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
      note("The final explicit hop must end in the configured destination. A selected custom address becomes the terminal hop automatically.")
    );

    addHopRow(
      rows,
      deterministicWallets,
      src.value,
      workload[0] || destinations[0],
      "2"
    );

    body.append(wal, amt, adv);
    refreshDeterministicWorkers();
  }

  function collectDeterministic() {
    const src = $("f-src").value;
    const destination = destinationEndpoint();

    if (destination.kind === "internal" && src === destination.value)
      throw new Error("Source and destination must be different wallets");

    const amount = toSats($("f-amt").value, "Amount");
    const delay = whole($("f-delay").value, "Wait between hops", 0);

    const flow = {
      description: $("f-desc").value.trim() || "dashboard run",
      source_wallet: src,
      flow_wallets: state.workers.filter(x => x.on).map(x => x.name),
      ...destinationConfig(),
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

      if (destination.kind === "external")
        flow.transfers[flow.transfers.length - 1].to = destination.value;

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
    const terminal = new Set(destinationSetInternalWallets());

    return workloadWallets().filter(
      w => w !== src && w !== stage && !terminal.has(w)
    );
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

    const destination = destinationSetControls(
      destinations,
      structural => {
        if (structural)
          experimentalWalletChanged();
        else
          refreshPreview();
      }
    );

    src.onchange = stage.onchange = experimentalWalletChanged;

    const topology = h("div");
    topology.id = "f-topology";

    const walletsBox = h("fieldset");
    walletsBox.append(
      h("legend", null, "Wallet roles"),
      field("Reserve / funding wallet", src),
      field("Allocation wallet", stage),
      destination.field,
      note("The reserve commits the full allocation to the allocation wallet before randomized workload begins. Terminal destinations are outside randomized routing.")
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
      note("The allocation wallet is the workload entry point. Diagonal worker cells are self-transfers. The reserve and all terminal destinations are excluded from experimental routing.")
    );

    body.append(walletsBox, workload, random, topo);

    resetDefaultEdges();
    drawTopology();
  }

  function collectExperimental() {
    const src = $("f-src").value;
    const stage = $("f-stage").value;
    const destinations = destinationSetConfig();
    const workers = experimentalWorkers();

    const terminalWallets = destinations.items
      .filter(item => item.type === "wallet")
      .map(item => item.wallet);

    if (terminalWallets.includes(src) || terminalWallets.includes(stage))
      throw new Error(
        "Reserve, allocation wallet, and terminal destination wallets "
        + "must be different"
      );

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
        finalization_wallet: stage,
        flow_wallets: [stage, ...workers],
        destinations,
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
        mode: "consolidate_then_distribute"
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
    const fanMode = $("f-play").value === "fan_out_fan_in";

    const terminal = fanMode
      ? new Set(
          $("f-dst-kind") && $("f-dst-kind").value === "internal" && $("f-dst")
            ? [$("f-dst").value]
            : []
        )
      : new Set(destinationSetInternalWallets());

    const keep = new Map(state.workers.map(x => [x.name, x.on]));

    const pool = $("f-play").value === "hub_and_spoke"
      ? roleWallets("workers")
      : workloadWallets();

    state.workers = pool
      .filter(w => w !== src && w !== stage && !terminal.has(w))
      .map(w => ({
        name: w,
        on: keep.has(w) ? keep.get(w) : false
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
    const destinations = destinationSetConfig();

    const terminalWallets = destinations.items
      .filter(item => item.type === "wallet")
      .map(item => item.wallet);

    if (terminalWallets.includes(src) || terminalWallets.includes(stage))
      throw new Error(
        "Reserve, allocation wallet, and terminal destination wallets "
        + "must be different"
      );

    const params = {
      source_wallet: src,
      allocation_wallet: stage,
      workers,
      ...destinationParams,
      allocation_sats: allocation,
      amount_sats_min: minAmount,
      amount_sats_max: maxAmount,
      delay_seconds_min: minDelay,
      delay_seconds_max: maxDelay,
      confirmations_required: whole($("f-conf").value, "Confirmations", 1),
      seed: whole($("f-seed").value, "Seed", 0)
    };

    if (!fanMode) {
      params.decisions = whole(
        $("f-jobs").value,
        "Number of decisions",
        1
      );
    }

    if ($("f-play").value === "hub_and_spoke") {
      const hub = $("f-hub");
      if (!hub || !hub.value)
        throw new Error("Hub-and-Spoke needs a hub wallet");
      params.hub_wallet = hub.value;
    }

    return params;
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
    const hubs = roleWallets("hubs");
    const destinations = roleWallets("destinations");

    const src = sel("f-src", reserves, reserves[0]);
    const stage = sel("f-stage", stages, stages[0]);
    const hub = sel("f-hub", hubs, hubs[0]);

    const destination = destinationSetControls(
      destinations,
      structural => {
        if (structural)
          refreshPlayWorkers();
        refreshPreview();
      }
    );

    const singleDestination = destinationControls(
      destinations,
      () => {
        refreshPlayWorkers();
        refreshPreview();
      }
    );

    const hubField = field("Hub wallet", hub);
    hubField.id = "f-hub-field";

    const workerBox = h("div", "row");
    workerBox.id = "f-play-workers";

    const workerControls = h("div", "row");

    const selectAllWorkers = h("button", null, "Select all");
    const clearWorkers = h("button", null, "Clear");
    selectAllWorkers.type = clearWorkers.type = "button";

    selectAllWorkers.onclick = () => {
      state.workers.forEach(w => { w.on = true; });
      drawPlayWorkers();
      refreshPreview();
    };

    clearWorkers.onclick = () => {
      state.workers.forEach(w => { w.on = false; });
      drawPlayWorkers();
      refreshPreview();
    };

    workerControls.append(selectAllWorkers, clearWorkers);

    const workerPicker = h("div");
    workerPicker.append(workerControls, workerBox);

    const roles = h("fieldset");
    roles.append(
      h("legend", null, "Play"),
      field("Strategy", play),
      field("Reserve / funding wallet", src),
      field("Allocation wallet", stage),
      hubField,
      field("Working wallets", workerPicker),
      ...singleDestination.fields,
      destination.field,
      note("Play topology and terminal distribution are compiled by FlowLab on the server. The resulting ordinary config is still reviewed, hashed, and approved before execution.")
    );

    const workload = h("fieldset");
    const decisionsField = field(
      "Number of decisions",
      num("f-jobs", "20", "100px")
    );

    workload.append(
      h("legend", null, "Play parameters"),
      field("Experiment allocation (DGB)", num("f-allocation", "2")),
      decisionsField,
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

    function refreshPlayShape() {
      const hubMode = play.value === "hub_and_spoke";
      const fanMode = play.value === "fan_out_fan_in";

      hubField.hidden = !hubMode;
      hub.disabled = !hubMode;

      singleDestination.fields.forEach(f => {
        f.hidden = !fanMode;
      });

      destination.field.hidden = fanMode;
      decisionsField.hidden = fanMode;

      refreshPlayWorkers();
      refreshPreview();
    }

    src.onchange = stage.onchange = hub.onchange = () => {
      refreshPlayWorkers();
      refreshPreview();
    };

    play.onchange = refreshPlayShape;

    refreshPlayShape();
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
    state.destinationRows = null;
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
