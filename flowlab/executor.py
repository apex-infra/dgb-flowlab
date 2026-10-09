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
    generate_settlement_cycle_phase_job,
    next_due,
    settlement_cycle_next_phase,
)
from .rpc import RpcError, to_sats
from .tx_builder import (
    BuildError,
    Prepared,
    broadcast,
    broadcast_distribution,
)
from .utxo_policy import resolve_policy

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
            self._ensure_experiment_start_cohorts(exp_id)

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

    def _is_settlement_cycle(self, flow):
        cfg = self._config_for_flow(flow)
        play = cfg.get("play") or {}

        return (
            play.get("name") == "settlement_cycle"
            and play.get("version") == 1
        )

    def _policy_uses_scope(self, flow, scope):
        cfg = self._config_for_flow(flow)
        policy = cfg["utxo_policy"]

        if policy["default"]["scope"] == scope:
            return True

        return any(
            rule["scope"] == scope
            for rule in policy["phases"].values()
        )

    @staticmethod
    def _cohort_wallets(flow):
        """Wallets that may act as senders inside this flow."""
        wallets = [
            flow["source_wallet"],
            *json.loads(flow["flow_wallets_json"]),
        ]

        # Preserve approved order while refusing accidental duplicates.
        return list(dict.fromkeys(wallets))

    def _capture_utxo_cohort(
            self,
            flow,
            scope,
            *,
            phase=None,
    ):
        """Capture and persist one immutable outpoint cohort."""
        existing = self.engine.get_utxo_cohort(
            flow["id"],
            scope,
            phase=phase,
        )

        if existing is not None:
            return existing

        wallets = {}

        for wallet in self._cohort_wallets(flow):
            coins = self.rpc.list_unspent(wallet, 0)

            outpoints = []

            for coin in coins:
                txid = coin.get("txid")
                vout = coin.get("vout")

                if (
                    not isinstance(txid, str)
                    or not txid
                    or not isinstance(vout, int)
                    or isinstance(vout, bool)
                    or vout < 0
                ):
                    raise PlanError(
                        f"invalid UTXO returned for wallet {wallet}"
                    )

                outpoints.append([txid, vout])

            outpoints.sort(
                key=lambda item: (item[0], item[1])
            )

            wallets[wallet] = outpoints

        cohort = {
            "captured_at": self.engine.now(),
            "wallets": wallets,
        }

        self.engine.save_utxo_cohort(
            flow["id"],
            scope,
            cohort,
            phase=phase,
        )

        return cohort

    def _ensure_experiment_start_cohorts(self, exp_id):
        """Persist every required experiment-start cohort before execution."""
        for flow in self.engine.list_flows(exp_id):
            if not self._policy_uses_scope(
                    flow,
                    "snapshot_at_start",
            ):
                continue

            # Settlement Cycle requires isolated worker/hub wallets.
            # Validate that isolation before persisting the immutable
            # experiment-start UTXO cohort so a refused dirty start
            # cannot poison a later resume with stale cohort state.
            if self._is_settlement_cycle(flow):
                self._settlement_cycle_preflight(flow)

            self._capture_utxo_cohort(
                flow,
                "snapshot_at_start",
            )

    def _ensure_phase_start_cohort(self, flow, job):
        """Capture a phase cohort immediately before its first execution."""
        phase = self._utxo_phase(job)
        cfg = self._config_for_flow(flow)

        rule = resolve_policy(
            cfg["utxo_policy"],
            phase,
        )

        if rule["scope"] != "snapshot_at_phase_start":
            return None

        return self._capture_utxo_cohort(
            flow,
            "snapshot_at_phase_start",
            phase=phase,
        )

    def _utxo_build_context(
            self,
            flow,
            job,
            source_wallet,
    ):
        """Resolve the immutable UTXO-selection context for one job."""
        cfg = self._config_for_flow(flow)
        phase = self._utxo_phase(job)

        rule = resolve_policy(
            cfg["utxo_policy"],
            phase,
        )

        cohort_outpoints = None
        scope = rule["scope"]

        if scope == "snapshot_at_start":
            cohort = self.engine.get_utxo_cohort(
                flow["id"],
                "snapshot_at_start",
            )

            if cohort is None:
                raise PlanError(
                    "required experiment-start UTXO cohort is missing"
                )

            wallets = cohort.get("wallets")

            if not isinstance(wallets, dict):
                raise PlanError(
                    "experiment-start UTXO cohort has invalid wallets"
                )

            rows = wallets.get(source_wallet)

            if not isinstance(rows, list):
                raise PlanError(
                    f"experiment-start UTXO cohort is missing "
                    f"source wallet {source_wallet}"
                )

            cohort_outpoints = {
                (row[0], row[1])
                for row in rows
                if (
                    isinstance(row, list)
                    and len(row) == 2
                    and isinstance(row[0], str)
                    and isinstance(row[1], int)
                    and not isinstance(row[1], bool)
                )
            }

            if len(cohort_outpoints) != len(rows):
                raise PlanError(
                    "experiment-start UTXO cohort contains "
                    "invalid outpoints"
                )

        elif scope == "snapshot_at_phase_start":
            cohort = self.engine.get_utxo_cohort(
                flow["id"],
                "snapshot_at_phase_start",
                phase=phase,
            )

            if cohort is None:
                raise PlanError(
                    f"required UTXO cohort for phase {phase!r} "
                    "is missing"
                )

            wallets = cohort.get("wallets")

            if not isinstance(wallets, dict):
                raise PlanError(
                    f"UTXO cohort for phase {phase!r} "
                    "has invalid wallets"
                )

            rows = wallets.get(source_wallet)

            if not isinstance(rows, list):
                raise PlanError(
                    f"UTXO cohort for phase {phase!r} is missing "
                    f"source wallet {source_wallet}"
                )

            cohort_outpoints = {
                (row[0], row[1])
                for row in rows
                if (
                    isinstance(row, list)
                    and len(row) == 2
                    and isinstance(row[0], str)
                    and isinstance(row[1], int)
                    and not isinstance(row[1], bool)
                )
            }

            if len(cohort_outpoints) != len(rows):
                raise PlanError(
                    f"UTXO cohort for phase {phase!r} contains "
                    "invalid outpoints"
                )

        elif scope != "live_wallet":
            raise PlanError(
                f"unsupported UTXO policy scope {scope!r}"
            )

        seed_material = (
            f"{flow['experiment_id']}:"
            f"{flow['id']}:"
            f"{job['seq']}:"
            f"{phase}:"
            f"{source_wallet}"
        )

        return {
            "utxo_policy": cfg["utxo_policy"],
            "phase": phase,
            "cohort_outpoints": cohort_outpoints,
            "seed_material": seed_material,
        }

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

    def _fan_out_fan_in_preflight(self, flow):
        """Require isolated worker wallets before Fan-Out/Fan-In V1."""
        cfg = self._config_for_flow(flow)
        play = cfg.get("play") or {}

        if not (
            play.get("name") == "fan_out_fan_in"
            and play.get("version") == 1
        ):
            return None

        approved_flow = self._approved_flow_config(flow)
        stage = approved_flow.get("allocation_wallet")

        if not isinstance(stage, str) or not stage:
            raise PlanError(
                "Fan-Out/Fan-In V1 requires an allocation wallet"
            )

        workers = [
            wallet
            for wallet in approved_flow["flow_wallets"]
            if wallet != stage
        ]

        if len(workers) < 2:
            raise PlanError(
                "Fan-Out/Fan-In V1 requires at least two workers"
            )

        contaminated = []

        for wallet in workers:
            mine = self.rpc.get_balances(wallet).get("mine", {})
            balances = {
                "trusted": to_sats(mine.get("trusted", 0)),
                "untrusted_pending": to_sats(
                    mine.get("untrusted_pending", 0)
                ),
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
                contaminated.append(f"{wallet} ({detail})")

        if contaminated:
            raise PlanError(
                "Fan-Out/Fan-In requires empty worker wallets "
                "before allocation: "
                + "; ".join(contaminated)
            )

        stage_mine = self.rpc.get_balances(stage).get("mine", {})

        stage_pending = to_sats(
            stage_mine.get("untrusted_pending", 0)
        )
        stage_immature = to_sats(
            stage_mine.get("immature", 0)
        )

        if stage_pending or stage_immature:
            raise PlanError(
                "Fan-Out/Fan-In requires the Stage starting balance "
                "to be fully confirmed "
                f"(untrusted_pending={stage_pending} sats, "
                f"immature={stage_immature} sats)"
            )

        stage_baseline_sats = to_sats(
            stage_mine.get("trusted", 0)
        )

        return {
            "stage_wallet": stage,
            "stage_baseline_sats": stage_baseline_sats,
        }

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

    def _settlement_cycle_preflight(self, flow):
        """Require isolated worker and hub wallets before Settlement Cycle V1."""
        cfg = self._config_for_flow(flow)
        play = cfg.get("play") or {}

        if not (
            play.get("name") == "settlement_cycle"
            and play.get("version") == 1
        ):
            return

        cycle = cfg.get("settlement_cycle") or {}

        wallets = list(dict.fromkeys([
            *cycle.get("workers", []),
            *cycle.get("hubs", []),
        ]))

        contaminated = []

        for wallet in wallets:
            mine = self.rpc.get_balances(wallet).get("mine", {})

            balances = {
                "trusted": to_sats(
                    mine.get("trusted", 0)
                ),
                "untrusted_pending": to_sats(
                    mine.get("untrusted_pending", 0)
                ),
                "immature": to_sats(
                    mine.get("immature", 0)
                ),
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
                contaminated.append(
                    f"{wallet} ({detail})"
                )

        if contaminated:
            raise PlanError(
                "Settlement Cycle requires empty worker and hub wallets "
                "before allocation: "
                + "; ".join(contaminated)
            )

    def _generate_next_settlement_cycle_job(self, flow):
        balances = self._confirmed_balances(flow)
        fee_reserve = self.builder.planning_fee_reserve_sats()

        phase = settlement_cycle_next_phase(
            self.engine,
            flow["id"],
            balances,
        )

        if phase == "consolidation":
            cfg = self._config_for_flow(flow)
            cycle = cfg["settlement_cycle"]

            internal_wallets = list(dict.fromkeys([
                *cycle["hubs"],
                *cycle["workers"],
            ]))

            sender = next(
                (
                    wallet
                    for wallet in internal_wallets
                    if balances.get(wallet, 0) > 0
                ),
                None,
            )

            if sender is not None:
                rule = resolve_policy(
                    cfg["utxo_policy"],
                    "consolidation",
                )

                cohort_outpoints = None
                can_measure_exact_inputs = (
                    rule["scope"] == "live_wallet"
                )

                if rule["scope"] == "snapshot_at_start":
                    cohort = self.engine.get_utxo_cohort(
                        flow["id"],
                        "snapshot_at_start",
                    )

                    if cohort is None:
                        raise PlanError(
                            "required experiment-start UTXO cohort "
                            "is missing"
                        )

                    wallets = cohort.get("wallets")

                    if not isinstance(wallets, dict):
                        raise PlanError(
                            "experiment-start UTXO cohort has "
                            "invalid wallets"
                        )

                    rows = wallets.get(sender)

                    if not isinstance(rows, list):
                        raise PlanError(
                            "experiment-start UTXO cohort is "
                            f"missing source wallet {sender}"
                        )

                    cohort_outpoints = {
                        (row[0], row[1])
                        for row in rows
                        if (
                            isinstance(row, list)
                            and len(row) == 2
                            and isinstance(row[0], str)
                            and isinstance(row[1], int)
                            and not isinstance(row[1], bool)
                        )
                    }

                    if len(cohort_outpoints) != len(rows):
                        raise PlanError(
                            "experiment-start UTXO cohort contains "
                            "invalid outpoints"
                        )

                    can_measure_exact_inputs = True

                # snapshot_at_phase_start intentionally remains on the
                # conservative generic reserve because its immutable cohort
                # is not captured until execution begins.
                if can_measure_exact_inputs:
                    fee_reserve = (
                        self.builder.planning_sweep_fee_reserve_sats(
                            sender,
                            utxo_policy=cfg["utxo_policy"],
                            phase="consolidation",
                            cohort_outpoints=cohort_outpoints,
                        )
                    )

        return generate_settlement_cycle_phase_job(
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
                play_context = self._fan_out_fan_in_preflight(flow)
                n = len(
                    generate_jobs(
                        e,
                        flow_id,
                        play_context=play_context,
                    )
                )
                actions.append(f"planned {n} jobs from the approved config")
        elif st == "PLAN":
            if self._is_settlement_cycle(flow):
                jobs = e.list_jobs(flow_id)

                if not jobs or all(
                    j["state"] == "CONFIRMED"
                    for j in jobs
                ):
                    balances = self._confirmed_balances(flow)
                    phase = settlement_cycle_next_phase(
                        e,
                        flow_id,
                        balances,
                    )

                    if phase == "complete":
                        raise EngineError(
                            "Settlement Cycle entered PLAN after completion"
                        )

                    if phase == "allocation":
                        self._settlement_cycle_preflight(flow)
                        jid = self._generate_allocation_job(flow)

                        if jid is None:
                            raise EngineError(
                                "Settlement Cycle allocation is incomplete "
                                "but no commitment job was generated"
                            )
                    else:
                        jid = self._generate_next_settlement_cycle_job(
                            flow
                        )

                    actions.append(
                        "generated Settlement Cycle "
                        f"{phase} step "
                        f"{self._step(e.get_job(jid))}"
                    )

            elif self._is_experimental(flow):
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

            if self._is_settlement_cycle(flow):
                balances = self._confirmed_balances(flow)
                phase = settlement_cycle_next_phase(
                    e,
                    flow_id,
                    balances,
                )

                target = (
                    "COMPLETE"
                    if phase == "complete"
                    else "PLAN"
                )

            elif self._is_experimental(flow):
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

    @staticmethod
    def _utxo_phase(job):
        """Return the UTXO-policy phase for one job.

        Jobs created before phase metadata existed are treated as ordinary
        workload jobs for backwards compatibility.
        """
        generated = json.loads(job["generated_from_json"] or "{}")
        phase = generated.get("phase", "workload")

        allowed = {
            "allocation",
            "workload",
            "consolidation",
            "finalization",
            "terminal_distribution",
            "settlement",
            "return_workload",
            "reserve_return",
        }

        if phase not in allowed:
            raise PlanError(
                f"job has unsupported UTXO policy phase {phase!r}"
            )

        return phase

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

        self._ensure_phase_start_cohort(
            flow,
            job,
        )

        utxo_context = self._utxo_build_context(
            flow,
            job,
            plan["from"],
        )

        # --------------------------------------------------
        # Settlement Cycle intermediate partial distribution.

        if "settlement" in plan:
            cfg = self._config_for_flow(flow)
            play = cfg.get("play") or {}

            if not (
                play.get("name") == "settlement_cycle"
                and play.get("version") == 1
            ):
                raise PlanError(
                    "settlement job is not authorized by a "
                    "Settlement Cycle V1 config"
                )

            cycle = cfg.get("settlement_cycle") or {}
            approved = cycle.get("settlement")

            if plan.get("settlement") != approved:
                raise PlanError(
                    "planned settlement differs from the approved "
                    "immutable settlement config"
                )

            if (
                not isinstance(approved, dict)
                or plan.get("from") != approved.get("source_wallet")
            ):
                raise PlanError(
                    "settlement must spend from the approved Stage wallet"
                )

            if plan.get("amount_sats") != "all":
                raise PlanError(
                    "settlement must consume the full selected "
                    "Stage settlement budget"
                )

            generated = json.loads(
                job["generated_from_json"] or "{}"
            )

            if (
                generated.get("phase") != "settlement"
                or generated.get("source")
                != "settlement cycle settlement"
            ):
                raise PlanError(
                    "settlement job has invalid persisted lifecycle metadata"
                )

            builder_items = []
            resolved = []
            external_addresses = set()

            for item in approved["items"]:
                kind = item["type"]

                if kind == "wallet":
                    wallet = item["wallet"]
                    address = self.rpc.get_new_address(
                        wallet,
                        "flowlab-settlement",
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
                        "approved settlement destination has an "
                        "unsupported type"
                    )

                builder_item = {
                    "address": address,
                }

                if approved["mode"] == "percentage":
                    builder_item["percent_bps"] = (
                        item["percent_bps"]
                    )
                else:
                    builder_item["amount_sats"] = (
                        item["amount_sats"]
                    )

                builder_items.append(builder_item)
                resolved.append(resolved_item)

            prepared = self.builder.build_partial_distribution(
                plan["from"],
                "all",
                approved["mode"],
                builder_items,
                allowed_external_addresses=external_addresses,
                **utxo_context,
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
                f"settlement broadcast {txid}"
            )
            return

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
                **utxo_context,
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

        generated = json.loads(
            job["generated_from_json"] or "{}"
        )
        play = self._config_for_flow(flow).get("play") or {}

        fan_out_fan_in_terminal = (
            play.get("name") == "fan_out_fan_in"
            and play.get("version") == 1
            and generated.get("phase") == "finalization"
            and plan.get("amount_sats") == "all"
        )

        if fan_out_fan_in_terminal:
            baseline = generated.get("stage_baseline_sats")
            stage_wallet = generated.get("stage_wallet")

            if (
                not isinstance(baseline, int)
                or isinstance(baseline, bool)
                or baseline < 0
            ):
                raise PlanError(
                    "Fan-Out/Fan-In finalization has an invalid "
                    "Stage baseline"
                )

            if stage_wallet != plan["from"]:
                raise PlanError(
                    "Fan-Out/Fan-In finalization Stage wallet "
                    "differs from the persisted Play context"
                )

            mine = self.rpc.get_balances(
                plan["from"]
            ).get("mine", {})

            current = to_sats(
                mine.get("trusted", 0)
            )

            if current < baseline:
                raise PlanError(
                    "Fan-Out/Fan-In Stage balance fell below its "
                    "starting baseline"
                )

            gross_budget = current - baseline

            if gross_budget <= 0:
                raise PlanError(
                    "Fan-Out/Fan-In has no experiment balance "
                    "available for finalization"
                )

            distribution = self.builder.build_distribution(
                plan["from"],
                gross_budget,
                "fixed",
                [{
                    "address": addr,
                    "remainder": True,
                }],
                allowed_external_addresses=(
                    {addr} if external else ()
                ),
                **utxo_context,
            )

            prepared = Prepared(
                source_wallet=distribution.source_wallet,
                address=addr,
                amount_sats=distribution.distributed_sats,
                change_address=distribution.change_address,
                hex=distribution.hex,
                txid=distribution.txid,
                fee_sats=distribution.fee_sats,
                inputs=distribution.inputs,
                outputs=distribution.outputs,
                utxo_selection=distribution.utxo_selection,
            )
        else:
            prepared = self.builder.build(
                plan["from"],
                addr,
                plan["amount_sats"],
                allow_external=external,
                **utxo_context,
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

            cfg = self._config_for_flow(flow)
            play = cfg.get("play") or {}
            settlement_cycle_v1 = (
                play.get("name") == "settlement_cycle"
                and play.get("version") == 1
            )
            settlement_cycle = (
                cfg.get("settlement_cycle") or {}
                if settlement_cycle_v1
                else {}
            )

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
                # Settlement Cycle intermediate partial distribution.

                if "settlement" in plan:
                    approved_spec = settlement_cycle.get(
                        "settlement"
                    )

                    if not settlement_cycle_v1:
                        issues.append(
                            f"{tag}: settlement job is not "
                            "authorized by Settlement Cycle V1"
                        )
                        continue

                    if plan.get("settlement") != approved_spec:
                        issues.append(
                            f"{tag}: planned settlement differs "
                            "from approved config"
                        )

                    if (
                        not isinstance(approved_spec, dict)
                        or plan.get("from")
                        != approved_spec.get("source_wallet")
                    ):
                        issues.append(
                            f"{tag}: settlement sender differs "
                            "from approved Stage wallet"
                        )

                    if plan.get("amount_sats") != "all":
                        issues.append(
                            f"{tag}: settlement is not a "
                            "full selected-budget send"
                        )

                    recorded_items = recorded.get(
                        "destinations"
                    )

                    if not isinstance(recorded_items, list):
                        issues.append(
                            f"{tag}: settlement has no recorded "
                            "destination list"
                        )
                        recorded_items = []

                    approved_items = (
                        approved_spec.get("items", [])
                        if isinstance(approved_spec, dict)
                        else []
                    )

                    if len(recorded_items) != len(
                        approved_items
                    ):
                        issues.append(
                            f"{tag}: recorded settlement destination "
                            "count differs from approved config"
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
                            f"{tag}: settlement has invalid "
                            "recorded budget"
                        )
                        budget = None

                    if (
                        not isinstance(distributed, int)
                        or isinstance(distributed, bool)
                        or distributed <= 0
                    ):
                        issues.append(
                            f"{tag}: settlement has invalid "
                            "recorded distributed amount"
                        )
                        distributed = None

                    expected_pairs = []
                    logical_ok = (
                        len(recorded_items)
                        == len(approved_items)
                        and budget is not None
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
                            if not isinstance(
                                recorded_item,
                                dict,
                            ):
                                issues.append(
                                    f"{tag}: settlement destination "
                                    f"{index} record is not an object"
                                )
                                logical_ok = False
                                continue

                            kind = approved_item["type"]

                            if (
                                recorded_item.get("type")
                                != kind
                            ):
                                issues.append(
                                    f"{tag}: settlement destination "
                                    f"{index} type differs from "
                                    "approved config"
                                )
                                logical_ok = False
                                continue

                            address = recorded_item.get(
                                "resolved_address"
                            )

                            if (
                                not isinstance(address, str)
                                or not address
                            ):
                                issues.append(
                                    f"{tag}: settlement destination "
                                    f"{index} has no resolved address"
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
                                        f"{tag}: settlement destination "
                                        f"{index} wallet differs from "
                                        "approved config"
                                    )
                                    logical_ok = False
                                    continue

                                try:
                                    owned = (
                                        self.rpc.get_address_info(
                                            wallet,
                                            address,
                                        ).get("ismine")
                                        is True
                                    )
                                except RpcError as err:
                                    issues.append(
                                        f"{tag}: cannot verify "
                                        f"settlement destination "
                                        f"{index} wallet address "
                                        f"({err})"
                                    )
                                    logical_ok = False
                                    continue

                                if not owned:
                                    issues.append(
                                        f"{tag}: settlement destination "
                                        f"{index} resolved address "
                                        f"is not owned by {wallet}"
                                    )
                                    logical_ok = False
                                    continue

                            elif kind == "address":
                                approved_address = (
                                    approved_item["address"]
                                )

                                if (
                                    recorded_item.get("address")
                                    != approved_address
                                    or address
                                    != approved_address
                                ):
                                    issues.append(
                                        f"{tag}: settlement destination "
                                        f"{index} external address "
                                        "differs from approved config"
                                    )
                                    logical_ok = False
                                    continue

                            else:
                                issues.append(
                                    f"{tag}: settlement destination "
                                    f"{index} has unsupported type"
                                )
                                logical_ok = False
                                continue

                            if (
                                approved_spec["mode"]
                                == "percentage"
                            ):
                                expected_amount = (
                                    budget
                                    * approved_item["percent_bps"]
                                ) // 10_000
                            else:
                                expected_amount = (
                                    approved_item["amount_sats"]
                                )

                            if (
                                recorded_item.get("amount_sats")
                                != expected_amount
                            ):
                                issues.append(
                                    f"{tag}: settlement destination "
                                    f"{index} recorded amount differs "
                                    "from approved settlement"
                                )
                                logical_ok = False

                            expected_pairs.append(
                                (
                                    address,
                                    expected_amount,
                                )
                            )

                    payout_total = (
                        sum(
                            amount
                            for _, amount
                            in expected_pairs
                        )
                        if logical_ok
                        else None
                    )

                    if (
                        distributed is not None
                        and payout_total is not None
                        and distributed != payout_total
                    ):
                        issues.append(
                            f"{tag}: settlement distributed amount "
                            f"{distributed} sats differs from "
                            f"approved payouts {payout_total} sats"
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
                            for output in raw.get(
                                "vout",
                                [],
                            )
                        ]

                    except RpcError as err:
                        issues.append(
                            f"{tag}: cannot decode settlement "
                            f"outputs ({err})"
                        )
                        actual_outputs = None

                    if (
                        actual_outputs is not None
                        and logical_ok
                        and budget is not None
                        and payout_total is not None
                    ):
                        actual_map = {}
                        duplicate = False

                        for address, sats in actual_outputs:
                            if address in actual_map:
                                duplicate = True
                            actual_map[address] = sats

                        if duplicate:
                            issues.append(
                                f"{tag}: settlement transaction "
                                "contains duplicate output addresses"
                            )

                        expected_map = dict(expected_pairs)

                        for address, amount in expected_pairs:
                            if actual_map.get(address) != amount:
                                issues.append(
                                    f"{tag}: settlement payout "
                                    f"to {address} differs from "
                                    "approved amount"
                                )

                        retained = [
                            (address, sats)
                            for address, sats
                            in actual_outputs
                            if address not in expected_map
                        ]

                        if len(retained) != 1:
                            issues.append(
                                f"{tag}: settlement transaction "
                                "must contain exactly one retained "
                                "Stage output"
                            )
                        else:
                            (
                                retained_address,
                                retained_sats,
                            ) = retained[0]

                            try:
                                retained_owned = (
                                    self.rpc.get_address_info(
                                        plan["from"],
                                        retained_address,
                                    ).get("ismine")
                                    is True
                                )
                            except RpcError as err:
                                issues.append(
                                    f"{tag}: cannot verify retained "
                                    f"Stage address ({err})"
                                )
                                retained_owned = False

                            if not retained_owned:
                                issues.append(
                                    f"{tag}: retained settlement "
                                    "output is not owned by Stage"
                                )

                            expected_retained = (
                                budget
                                - payout_total
                                - fee
                            )

                            if (
                                retained_sats
                                != expected_retained
                            ):
                                issues.append(
                                    f"{tag}: retained Stage output "
                                    f"was {retained_sats} sats, "
                                    f"expected "
                                    f"{expected_retained}"
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

            if settlement_cycle_v1:
                settlement = settlement_cycle.get(
                    "settlement"
                ) or {}

                managed_wallets.extend(
                    item["wallet"]
                    for item in settlement.get(
                        "items",
                        [],
                    )
                    if item.get("type") == "wallet"
                )

            elif multi_destination:
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
