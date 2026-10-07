"""
executor.py -- runs the operator's approved transfer list, one safe step at a time.

tick() makes at most one state change per flow and then returns, so it can be
called in a loop (or by hand). Every action still goes through the engine's
verification and journal. Anything unexpected pauses the experiment; nothing
is retried automatically.
"""

import json

from .config_schema import (
    destination_endpoint,
    destination_identity,
    destination_is_external,
    destination_wallets,
    has_multi_destinations,
)
from .engine import EngineError, GuardFailed
from .planner import (
    PlanError,
    experimental_allocation_jobs,
    experimental_workload_jobs,
    generate_experimental_allocation_job,
    generate_experimental_finalization_job,
    generate_experimental_job,
    generate_jobs,
    next_due,
)
from .rpc import RpcError, to_sats
from .tx_builder import (
    BuildError,
    broadcast,
    broadcast_distribution,
)

POLL_SECONDS = 15


class Executor:
    def __init__(self, engine, rpc, builder, log=lambda msg: None):
        self.engine, self.rpc, self.builder, self.log = engine, rpc, builder, log

    # ------------------------------------------------------------------ tick
    def tick(self, exp_id):
        e = self.engine
        exp = e.get_experiment(exp_id)
        out = {"actions": [], "wait_s": None, "blocked": None, "done": False}
        if exp["state"] != "RUNNING":
            out["blocked"] = f"experiment is {exp['state']}"
            return out
        waits = []
        try:
            for flow in e.list_flows(exp_id):
                self._tick_flow(exp_id, flow["id"], out["actions"], waits)
            flows = e.list_flows(exp_id)
            if flows and all(f["state"] == "COMPLETE" for f in flows):
                e.begin_completion(exp_id)
                ok, final, stats = self.reconcile(exp_id)
                done_ok = e.complete_experiment(exp_id, ok, final, stats)
                out["actions"].append("completed and reconciled" if done_ok
                                      else f"RECONCILIATION FAILED: {stats['issues']}")
                out["done"] = True
        except GuardFailed as g:
            out["blocked"] = f"verification failed: {g}"
            self._pause(exp_id, out["blocked"])
        except (BuildError, RpcError, PlanError, EngineError) as err:
            out["blocked"] = f"{type(err).__name__}: {err}"
            self._pause(exp_id, out["blocked"])
        if waits and not out["blocked"]:
            out["wait_s"] = min(waits)
        for a in out["actions"]:
            self.log(a)
        return out

    def _pause(self, exp_id, reason):
        if self.engine.get_experiment(exp_id)["state"] == "RUNNING":
            self.engine.pause(exp_id, reason[:300])

    # ---------------------------------------------------- experiment planning
    def _config_for_flow(self, flow):
        exp = self.engine.get_experiment(flow["experiment_id"])
        return json.loads(exp["config_json"])

    def _is_experimental(self, flow):
        cfg = self._config_for_flow(flow)
        return bool(cfg.get("randomization", {}).get("enabled"))

    def _approved_flow_config(self, flow):
        cfg = self._config_for_flow(flow)

        matches = [
            fl for fl in cfg["flows"]
            if fl["source_wallet"] == flow["source_wallet"]
            and destination_identity(fl) == flow["destination_wallet"]
        ]

        if len(matches) != 1:
            raise PlanError(
                "cannot match this flow to exactly one flow in the approved config"
            )

        return matches[0]

    def _experimental_allocation_complete(self, flow):
        approved_flow = self._approved_flow_config(flow)

        if approved_flow.get("allocation_wallet") is None:
            return True

        jobs = experimental_allocation_jobs(self.engine, flow["id"])
        if len(jobs) > 1:
            raise PlanError("experimental flow has more than one allocation commitment")

        return (
            len(jobs) == 1
            and jobs[0]["state"] == "CONFIRMED"
        )

    def _experimental_workload_complete(self, flow):
        cfg = self._config_for_flow(flow)
        workload = cfg.get("workload") or {}
        if workload.get("mode") != "count":
            raise PlanError("executor v1 currently supports count experimental workloads only")
        jobs = experimental_workload_jobs(self.engine, flow["id"])
        return (
            len(jobs) >= workload["jobs"]
            and all(j["state"] == "CONFIRMED" for j in jobs)
        )

    def _experimental_workers_empty(self, flow):
        balances = self._confirmed_balances(flow)
        workers = json.loads(flow["flow_wallets_json"])
        return all(balances.get(worker, 0) == 0 for worker in workers)

    def _confirmed_balances(self, flow):
        wallets = [
            flow["source_wallet"],
            *json.loads(flow["flow_wallets_json"]),
        ]
        return {
            wallet: to_sats(self.rpc.get_balances(wallet)["mine"]["trusted"])
            for wallet in wallets
        }

    def _generate_allocation_job(self, flow):
        approved_flow = self._approved_flow_config(flow)

        allocation_wallet = approved_flow.get("allocation_wallet")

        if allocation_wallet is not None:
            mine = self.rpc.get_balances(allocation_wallet).get("mine", {})

            balances = {
                "trusted": to_sats(mine.get("trusted", 0)),
                "untrusted_pending": to_sats(mine.get("untrusted_pending", 0)),
                "immature": to_sats(mine.get("immature", 0)),
            }

            nonzero = {
                name: sats
                for name, sats in balances.items()
                if sats != 0
            }

            if nonzero:
                detail = ", ".join(
                    f"{name}={sats} sats"
                    for name, sats in nonzero.items()
                )
                raise PlanError(
                    f"allocation wallet {allocation_wallet} must be empty "
                    f"before commitment ({detail})"
                )

        return generate_experimental_allocation_job(
            self.engine,
            flow["id"],
        )

    def _generate_next_experimental_job(self, flow):
        balances = self._confirmed_balances(flow)
        fee_reserve = self.builder.planning_fee_reserve_sats()
        return generate_experimental_job(
            self.engine,
            flow["id"],
            balances,
            fee_reserve_sats=fee_reserve,
        )

    def _generate_next_finalization_job(self, flow):
        balances = self._confirmed_balances(flow)
        fee_reserve = self.builder.planning_fee_reserve_sats()
        return generate_experimental_finalization_job(
            self.engine,
            flow["id"],
            balances,
            fee_reserve_sats=fee_reserve,
        )

    # ------------------------------------------------------------ flow steps
    def _tick_flow(self, exp_id, flow_id, actions, waits):
        e = self.engine
        flow = e.get_flow(flow_id)
        st = flow["state"]
        if flow["error_state"] != "NONE":
            raise EngineError(f"flow {flow_id} is in error state {flow['error_state']}")
        if st == "START":
            e.advance_flow(flow_id, "PLAN")
            if self._is_experimental(flow):
                actions.append("experimental flow entered progressive planning")
            else:
                n = len(generate_jobs(e, flow_id))
                actions.append(f"planned {n} jobs from the approved config")
        elif st == "PLAN":
            if self._is_experimental(flow):
                jobs = e.list_jobs(flow_id)
                if not jobs or all(j["state"] == "CONFIRMED" for j in jobs):
                    if not self._experimental_allocation_complete(flow):
                        jid = self._generate_allocation_job(flow)
                        if jid is None:
                            raise EngineError(
                                "experimental allocation is incomplete but no commitment job was generated"
                            )
                        actions.append(
                            f"generated allocation step {self._step(e.get_job(jid))}"
                        )
                    elif not self._experimental_workload_complete(flow):
                        jid = self._generate_next_experimental_job(flow)
                        actions.append(
                            f"generated experimental step {self._step(e.get_job(jid))}"
                        )
                    else:
                        jid = self._generate_next_finalization_job(flow)
                        if jid is None:
                            raise EngineError(
                                "experimental finalization entered PLAN with no worker to sweep"
                            )
                        actions.append(
                            f"generated finalization step {self._step(e.get_job(jid))}"
                        )
            r = next_due(e, flow_id)
            if r["job"]:
                e.advance_flow(flow_id, "EXECUTE")
                actions.append(f"step {self._step(r['job'])} is due")
            elif r["wait_s"]:
                waits.append(r["wait_s"])
            else:
                raise EngineError(f"nothing runnable in PLAN: {r['reason']}")
        elif st == "EXECUTE":
            self._execute(flow_id, actions)
        elif st == "CONFIRMATION":
            self._confirm(flow, actions, waits)
        elif st == "NEXT_STATE":
            jobs = e.list_jobs(flow_id)
            if self._is_experimental(flow):
                if not self._experimental_allocation_complete(flow):
                    target = "PLAN"
                elif not self._experimental_workload_complete(flow):
                    target = "PLAN"
                elif self._experimental_workers_empty(flow):
                    target = "COMPLETE"
                else:
                    target = "PLAN"
            else:
                target = "COMPLETE" if all(j["state"] == "CONFIRMED" for j in jobs) else "PLAN"
            e.advance_flow(flow_id, target)

    @staticmethod
    def _step(job):
        return json.loads(job["planned_json"])["step"] + 1

    def _execute(self, flow_id, actions):
        e = self.engine
        jobs = [
            j for j in e.list_jobs(flow_id)
            if j["state"] == "PLANNED"
        ]

        if not jobs:
            raise EngineError(
                "EXECUTE with no planned job"
            )

        job = jobs[0]

        live = e.conn.execute(
            "SELECT 1 FROM action_journal "
            "WHERE job_id=? "
            "AND status IN ('intent','unknown')",
            (job["id"],),
        ).fetchone()

        if live:
            raise EngineError(
                "an earlier broadcast for this job is unresolved; "
                "resolve it first"
            )

        plan = json.loads(job["planned_json"])
        flow = e.get_flow(flow_id)
        approved_flow = self._approved_flow_config(flow)

        # --------------------------------------------------
        # Multi-destination terminal transaction.

        if "distribution" in plan:
            if not has_multi_destinations(approved_flow):
                raise PlanError(
                    "distribution job is not authorized by the "
                    "approved flow config"
                )

            if plan.get("distribution") != approved_flow["destinations"]:
                raise PlanError(
                    "planned terminal distribution differs from "
                    "the approved immutable config"
                )

            if (
                plan.get("from")
                != approved_flow["finalization_wallet"]
            ):
                raise PlanError(
                    "terminal distribution must spend from the "
                    "approved finalization wallet"
                )

            if plan.get("amount_sats") != "all":
                raise PlanError(
                    "terminal distribution must consume the full "
                    "finalization-wallet balance"
                )

            spec = approved_flow["destinations"]
            builder_items = []
            resolved = []
            external_addresses = set()

            for item in spec["items"]:
                kind = item["type"]

                if kind == "wallet":
                    wallet = item["wallet"]
                    address = self.rpc.get_new_address(
                        wallet,
                        "flowlab-final",
                    )

                    resolved_item = {
                        "type": "wallet",
                        "wallet": wallet,
                        "resolved_address": address,
                    }

                elif kind == "address":
                    address = item["address"]
                    external_addresses.add(address)

                    resolved_item = {
                        "type": "address",
                        "address": address,
                        "resolved_address": address,
                    }

                else:
                    raise PlanError(
                        "approved terminal destination has an "
                        "unsupported type"
                    )

                builder_item = {
                    "address": address,
                }

                if spec["mode"] == "percentage":
                    builder_item["percent_bps"] = (
                        item["percent_bps"]
                    )

                elif item.get("remainder"):
                    builder_item["remainder"] = True

                else:
                    builder_item["amount_sats"] = (
                        item["amount_sats"]
                    )

                builder_items.append(builder_item)
                resolved.append(resolved_item)

            prepared = self.builder.build_distribution(
                plan["from"],
                "all",
                spec["mode"],
                builder_items,
                allowed_external_addresses=external_addresses,
            )

            self.log(
                "PREVIEW\n" + prepared.summary()
            )

            txid = broadcast_distribution(
                e,
                self.rpc,
                job["id"],
                prepared,
                resolved,
            )

            e.advance_flow(
                flow_id,
                "CONFIRMATION",
            )

            actions.append(
                f"step {self._step(job)} "
                f"terminal distribution broadcast {txid}"
            )
            return

        # --------------------------------------------------
        # Existing single-output path.

        external = (
            destination_is_external(approved_flow)
            and plan["to"] == destination_endpoint(approved_flow)
        )

        if external:
            addr = destination_endpoint(approved_flow)
        else:
            addr = self.rpc.get_new_address(
                plan["to"],
                "flowlab-recv",
            )

        prepared = self.builder.build(
            plan["from"],
            addr,
            plan["amount_sats"],
            allow_external=external,
        )

        self.log(
            "PREVIEW\n" + prepared.summary()
        )

        txid = broadcast(
            e,
            self.rpc,
            job["id"],
            prepared,
        )

        e.advance_flow(
            flow_id,
            "CONFIRMATION",
        )

        actions.append(
            f"step {self._step(job)} broadcast {txid}"
        )

    def _confirm(self, flow, actions, waits):
        e = self.engine
        jobs = e.list_jobs(flow["id"])
        pending = [j for j in jobs if j["state"] == "BROADCAST"]
        if not pending:
            e.advance_flow(flow["id"], "NEXT_STATE")
            return
        job = pending[0]
        plan = json.loads(job["planned_json"])
        info = self.rpc.get_transaction(plan["from"], job["txid"])
        confs = max(0, int(info.get("confirmations", 0)))
        e.record_confirmation(job["txid"], confs, info.get("blockheight"))
        if e.get_job(job["id"])["state"] == "CONFIRMED":
            e.advance_flow(flow["id"], "NEXT_STATE")
            actions.append(f"step {self._step(job)} confirmed ({confs} confirmations)")
        else:
            waits.append(POLL_SECONDS)

    # -------------------------------------------------------- reconciliation
    def reconcile(self, exp_id):
        """Check every transfer against sender records and approved endpoints."""
        e = self.engine
        issues, fees, moved, balances, njobs = [], 0, 0, {}, 0

        for flow in e.list_flows(exp_id):
            approved_flow = self._approved_flow_config(flow)

            multi_destination = has_multi_destinations(
                approved_flow
            )

            external_destination = (
                None
                if multi_destination
                else (
                    destination_endpoint(approved_flow)
                    if destination_is_external(approved_flow)
                    else None
                )
            )

            for job in e.list_jobs(flow["id"]):
                njobs += 1
                plan = json.loads(job["planned_json"])
                recorded = json.loads(
                    job["result_json"] or "{}"
                )
                tag = f"step {plan['step'] + 1}"

                try:
                    sent = self.rpc.get_transaction(
                        plan["from"],
                        job["txid"],
                    )
                except RpcError as err:
                    issues.append(
                        f"{tag}: cannot read sender transaction "
                        f"({err})"
                    )
                    continue

                fee = -to_sats(sent.get("fee", 0))

                # ------------------------------------------
                # Multi-output terminal distribution.

                if "distribution" in plan:
                    if not multi_destination:
                        issues.append(
                            f"{tag}: distribution job is not "
                            "authorized by approved config"
                        )
                        continue

                    approved_spec = approved_flow["destinations"]

                    if plan["distribution"] != approved_spec:
                        issues.append(
                            f"{tag}: planned terminal distribution "
                            "differs from approved config"
                        )

                    if (
                        plan.get("from")
                        != approved_flow["finalization_wallet"]
                    ):
                        issues.append(
                            f"{tag}: terminal distribution sender "
                            "differs from approved finalization wallet"
                        )

                    if plan.get("amount_sats") != "all":
                        issues.append(
                            f"{tag}: terminal distribution is not "
                            "a full-balance send"
                        )

                    recorded_items = recorded.get(
                        "destinations"
                    )

                    if not isinstance(recorded_items, list):
                        issues.append(
                            f"{tag}: terminal distribution has no "
                            "recorded destination list"
                        )
                        recorded_items = []

                    approved_items = approved_spec["items"]

                    if len(recorded_items) != len(approved_items):
                        issues.append(
                            f"{tag}: recorded terminal destination "
                            "count differs from approved config"
                        )

                    builder_items = []
                    resolved_addresses = []
                    logical_ok = (
                        len(recorded_items)
                        == len(approved_items)
                    )

                    if logical_ok:
                        for index, (
                            approved_item,
                            recorded_item,
                        ) in enumerate(
                            zip(
                                approved_items,
                                recorded_items,
                            ),
                            1,
                        ):
                            if not isinstance(recorded_item, dict):
                                issues.append(
                                    f"{tag}: destination {index} "
                                    "record is not an object"
                                )
                                logical_ok = False
                                continue

                            kind = approved_item["type"]

                            if recorded_item.get("type") != kind:
                                issues.append(
                                    f"{tag}: destination {index} "
                                    "type differs from approved config"
                                )
                                logical_ok = False
                                continue

                            resolved_address = (
                                recorded_item.get(
                                    "resolved_address"
                                )
                            )

                            if (
                                not isinstance(
                                    resolved_address,
                                    str,
                                )
                                or not resolved_address
                            ):
                                issues.append(
                                    f"{tag}: destination {index} "
                                    "has no resolved address"
                                )
                                logical_ok = False
                                continue

                            if kind == "wallet":
                                wallet = approved_item["wallet"]

                                if (
                                    recorded_item.get("wallet")
                                    != wallet
                                ):
                                    issues.append(
                                        f"{tag}: destination "
                                        f"{index} wallet differs "
                                        "from approved config"
                                    )
                                    logical_ok = False
                                    continue

                                try:
                                    owned = (
                                        self.rpc.get_address_info(
                                            wallet,
                                            resolved_address,
                                        ).get("ismine")
                                        is True
                                    )
                                except RpcError as err:
                                    issues.append(
                                        f"{tag}: cannot verify "
                                        f"destination {index} "
                                        f"wallet address ({err})"
                                    )
                                    logical_ok = False
                                    continue

                                if not owned:
                                    issues.append(
                                        f"{tag}: destination "
                                        f"{index} resolved address "
                                        f"is not owned by {wallet}"
                                    )
                                    logical_ok = False
                                    continue

                            elif kind == "address":
                                address = approved_item["address"]

                                if (
                                    recorded_item.get("address")
                                    != address
                                    or resolved_address != address
                                ):
                                    issues.append(
                                        f"{tag}: destination "
                                        f"{index} external address "
                                        "differs from approved config"
                                    )
                                    logical_ok = False
                                    continue

                            else:
                                issues.append(
                                    f"{tag}: destination {index} "
                                    "has unsupported type"
                                )
                                logical_ok = False
                                continue

                            item = {
                                "address": resolved_address,
                            }

                            if (
                                approved_spec["mode"]
                                == "percentage"
                            ):
                                item["percent_bps"] = (
                                    approved_item[
                                        "percent_bps"
                                    ]
                                )
                            elif approved_item.get(
                                "remainder"
                            ):
                                item["remainder"] = True
                            else:
                                item["amount_sats"] = (
                                    approved_item[
                                        "amount_sats"
                                    ]
                                )

                            builder_items.append(item)
                            resolved_addresses.append(
                                resolved_address
                            )

                    budget = recorded.get("budget_sats")
                    distributed = recorded.get(
                        "distributed_sats"
                    )

                    if (
                        not isinstance(budget, int)
                        or isinstance(budget, bool)
                        or budget <= 0
                    ):
                        issues.append(
                            f"{tag}: terminal distribution has "
                            "invalid recorded budget"
                        )
                        budget = None

                    if (
                        not isinstance(distributed, int)
                        or isinstance(distributed, bool)
                        or distributed <= 0
                    ):
                        issues.append(
                            f"{tag}: terminal distribution has "
                            "invalid recorded distributed amount"
                        )
                        distributed = None

                    if (
                        budget is not None
                        and distributed is not None
                        and distributed != budget - fee
                    ):
                        issues.append(
                            f"{tag}: distributed amount "
                            f"{distributed} sats does not equal "
                            f"budget {budget} minus fee {fee}"
                        )

                    expected_pairs = None

                    if (
                        logical_ok
                        and distributed is not None
                    ):
                        try:
                            expected_pairs = (
                                self.builder
                                ._distribution_amounts(
                                    approved_spec["mode"],
                                    builder_items,
                                    distributed,
                                )
                            )
                        except BuildError as err:
                            issues.append(
                                f"{tag}: cannot recompute "
                                f"approved terminal distribution "
                                f"({err})"
                            )

                    if expected_pairs is not None:
                        recorded_pairs = [
                            (
                                item.get(
                                    "resolved_address"
                                ),
                                item.get("amount_sats"),
                            )
                            for item in recorded_items
                        ]

                        if recorded_pairs != expected_pairs:
                            issues.append(
                                f"{tag}: recorded destination "
                                "amounts differ from the approved "
                                "distribution"
                            )

                    try:
                        txhex = sent.get("hex")

                        if not txhex:
                            raise RpcError(
                                "wallet transaction has no raw hex"
                            )

                        raw = self.rpc.decode_raw_transaction(
                            txhex
                        )

                        actual_outputs = [
                            (
                                output.get(
                                    "scriptPubKey",
                                    {},
                                ).get("address"),
                                to_sats(output["value"]),
                            )
                            for output in raw.get("vout", [])
                        ]

                    except RpcError as err:
                        issues.append(
                            f"{tag}: cannot decode terminal "
                            f"distribution outputs ({err})"
                        )
                        actual_outputs = None

                    if (
                        actual_outputs is not None
                        and expected_pairs is not None
                    ):
                        if (
                            len(actual_outputs)
                            != len(expected_pairs)
                        ):
                            issues.append(
                                f"{tag}: terminal transaction "
                                "has an unexpected number of "
                                "outputs"
                            )
                        else:
                            actual_map = {}
                            duplicate = False

                            for address, sats in actual_outputs:
                                if address in actual_map:
                                    duplicate = True
                                actual_map[address] = sats

                            if duplicate:
                                issues.append(
                                    f"{tag}: terminal transaction "
                                    "contains duplicate output "
                                    "addresses"
                                )

                            if actual_map != dict(
                                expected_pairs
                            ):
                                issues.append(
                                    f"{tag}: terminal transaction "
                                    "outputs differ from approved "
                                    "distribution"
                                )

                    confirmations = sent.get(
                        "confirmations",
                        0,
                    )

                    if fee != recorded.get("fee_sats"):
                        issues.append(
                            f"{tag}: fee {fee} sats differs "
                            "from the recorded "
                            f"{recorded.get('fee_sats')}"
                        )

                    if (
                        confirmations
                        < flow["confirmations_required"]
                    ):
                        issues.append(
                            f"{tag}: below the required "
                            "confirmations"
                        )

                    fees += fee
                    moved += distributed or 0
                    continue

                # ------------------------------------------
                # Existing single-output reconciliation.

                want = (
                    recorded.get("amount_sats")
                    if plan["amount_sats"] == "all"
                    else plan["amount_sats"]
                )

                external = (
                    external_destination is not None
                    and plan["to"] == external_destination
                )

                if external or plan["from"] == plan["to"]:
                    address = (
                        external_destination
                        if external
                        else recorded.get("address")
                    )

                    if not address:
                        issues.append(
                            f"{tag}: transaction has no "
                            "recorded destination address"
                        )
                    else:
                        try:
                            txhex = sent.get("hex")

                            if not txhex:
                                raise RpcError(
                                    "wallet transaction has no "
                                    "raw hex"
                                )

                            raw = (
                                self.rpc
                                .decode_raw_transaction(txhex)
                            )

                            actual = sum(
                                to_sats(o["value"])
                                for o in raw.get("vout", [])
                                if o.get(
                                    "scriptPubKey",
                                    {},
                                ).get("address")
                                == address
                            )

                        except RpcError as err:
                            kind = (
                                "external"
                                if external
                                else "self-transfer"
                            )

                            issues.append(
                                f"{tag}: cannot decode "
                                f"{kind} output ({err})"
                            )
                            actual = None

                        if (
                            actual is not None
                            and (
                                want is None
                                or actual != want
                            )
                        ):
                            kind = (
                                "external"
                                if external
                                else "self-transfer"
                            )

                            issues.append(
                                f"{tag}: {kind} output was "
                                f"{actual} sats, expected {want}"
                            )

                    confirmations = sent.get(
                        "confirmations",
                        0,
                    )

                else:
                    try:
                        got = self.rpc.get_transaction(
                            plan["to"],
                            job["txid"],
                        )
                    except RpcError as err:
                        issues.append(
                            f"{tag}: cannot read receiver "
                            f"transaction ({err})"
                        )
                        continue

                    if (
                        want is None
                        or to_sats(got["amount"]) != want
                    ):
                        issues.append(
                            f"{tag}: receiver saw "
                            f"{to_sats(got['amount'])} sats, "
                            f"expected {want}"
                        )

                    confirmations = got.get(
                        "confirmations",
                        0,
                    )

                if fee != recorded.get("fee_sats"):
                    issues.append(
                        f"{tag}: fee {fee} sats differs "
                        "from the recorded "
                        f"{recorded.get('fee_sats')}"
                    )

                if (
                    confirmations
                    < flow["confirmations_required"]
                ):
                    issues.append(
                        f"{tag}: below the required "
                        "confirmations"
                    )

                fees += fee
                moved += want or 0

            managed_wallets = [
                flow["source_wallet"],
                *json.loads(flow["flow_wallets_json"]),
            ]

            if multi_destination:
                managed_wallets.extend(
                    destination_wallets(approved_flow)
                )
            elif not destination_is_external(
                approved_flow
            ):
                managed_wallets.append(
                    flow["destination_wallet"]
                )

            for wallet in dict.fromkeys(
                managed_wallets
            ):
                balances[wallet] = to_sats(
                    self.rpc.get_balances(
                        wallet
                    )["mine"]["trusted"]
                )

        stats = {
            "jobs": njobs,
            "total_fees_sats": fees,
            "total_moved_sats": moved,
            "issues": issues,
        }

        return (
            not issues
        ), {
            "balances_sats": balances
        }, stats
