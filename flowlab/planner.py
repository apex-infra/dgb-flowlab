"""
planner.py -- turns the operator's explicit transfer list into jobs.

Nothing here chooses anything. Every hop (from, to, amount, delay) is written
by the operator in the approved configuration; the planner only creates the
jobs in that exact order, each depending on the previous one being CONFIRMED,
and reports which one (if any) is allowed to run now.
"""

import hashlib
import json
import random
from datetime import datetime, timedelta

from .config_schema import (
    destination_identity,
    has_multi_destinations,
)


GENERATOR_VERSION = 1


class PlanError(Exception):
    pass


def transfers_for(engine, flow_id):
    flow = engine.get_flow(flow_id)
    cfg = json.loads(engine.get_experiment(flow["experiment_id"])["config_json"])
    match = [f for f in cfg["flows"] if f["source_wallet"] == flow["source_wallet"]
             and destination_identity(f) == flow["destination_wallet"]]
    if len(match) != 1:
        raise PlanError("cannot match this flow to exactly one flow in the approved config")
    transfers = match[0].get("transfers")
    if not transfers:
        raise PlanError("this flow has no explicit transfers in its approved config")
    return transfers


def experimental_decision(
        engine,
        flow_id,
        decision_index,
        balances_sats=None,
        fee_reserve_sats=0,
        source_budget_remaining_sats=None,
):
    """Return one reproducible experimental decision.

    balances_sats, when supplied, is a caller-provided snapshot of confirmed
    wallet balances. The planner itself remains pure: no RPC, UTXO reads,
    job creation, or broadcasting occurs here.
    """
    if not isinstance(decision_index, int) or isinstance(decision_index, bool) or decision_index < 0:
        raise PlanError("decision_index must be a non-negative integer")
    if (not isinstance(fee_reserve_sats, int) or isinstance(fee_reserve_sats, bool)
            or fee_reserve_sats < 0):
        raise PlanError("fee_reserve_sats must be a non-negative integer")
    if source_budget_remaining_sats is not None:
        if (not isinstance(source_budget_remaining_sats, int)
                or isinstance(source_budget_remaining_sats, bool)
                or source_budget_remaining_sats < 0):
            raise PlanError(
                "source_budget_remaining_sats must be a non-negative integer or None"
            )

    flow = engine.get_flow(flow_id)
    cfg = json.loads(engine.get_experiment(flow["experiment_id"])["config_json"])

    matches = [
        (i, f) for i, f in enumerate(cfg["flows"])
        if f["source_wallet"] == flow["source_wallet"]
        and destination_identity(f) == flow["destination_wallet"]
    ]
    if len(matches) != 1:
        raise PlanError("cannot match this flow to exactly one flow in the approved config")

    flow_index, _ = matches[0]
    rnd = cfg["randomization"]
    if not rnd.get("enabled"):
        raise PlanError("experimental decision requested but randomization is disabled")
    if rnd.get("model") not in ("uniform", "seeded_deterministic"):
        raise PlanError(f"randomization model {rnd.get('model')!r} is not implemented by generator v1")

    workload = cfg.get("workload")
    if not workload:
        raise PlanError("experimental decision requires a workload envelope")

    material = (
        f"flowlab-generator-v{GENERATOR_VERSION}:"
        f"{rnd['seed']}:{flow_index}:{decision_index}"
    ).encode()
    derived_seed = int.from_bytes(hashlib.sha256(material).digest(), "big")
    rng = random.Random(derived_seed)

    topology = matches[0][1].get("experimental_topology")
    if not topology:
        raise PlanError("experimental decision requires an approved topology")

    transitions = topology["transitions"]

    if balances_sats is None:
        eligible = list(transitions)
    else:
        if not isinstance(balances_sats, dict):
            raise PlanError("balances_sats must be a wallet -> integer sats mapping")
        for wallet, value in balances_sats.items():
            if not isinstance(wallet, str) or not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PlanError("balances_sats must contain non-negative integer satoshi balances")

        minimum = workload["amount_sats_min"]

        def spendable(t):
            amount = balances_sats.get(t["from"], 0) - fee_reserve_sats
            if (
                t["from"] == flow["source_wallet"]
                and source_budget_remaining_sats is not None
            ):
                amount = min(amount, source_budget_remaining_sats)
            return amount

        eligible = [t for t in transitions if spendable(t) >= minimum]

    if not eligible:
        raise PlanError("no approved experimental transition currently has enough confirmed balance")

    route = eligible[rng.randrange(len(eligible))]

    if balances_sats is None:
        available = workload["amount_sats_max"]
    else:
        available = min(
            workload["amount_sats_max"],
            balances_sats.get(route["from"], 0) - fee_reserve_sats,
        )
        if (
            route["from"] == flow["source_wallet"]
            and source_budget_remaining_sats is not None
        ):
            available = min(available, source_budget_remaining_sats)
    if available < workload["amount_sats_min"]:
        raise PlanError("selected route cannot satisfy the approved minimum amount")

    amount = rng.randint(
        workload["amount_sats_min"],
        available,
    )
    delay = rng.randint(
        workload["delay_seconds_min"],
        workload["delay_seconds_max"],
    )

    return {
        "from": route["from"],
        "to": route["to"],
        "amount_sats": amount,
        "delay_seconds": delay,
        "generated_from": {
            "source": EXPERIMENTAL_JOB_SOURCE,
            "generator_version": GENERATOR_VERSION,
            "model": rnd["model"],
            "seed": rnd["seed"],
            "flow_index": flow_index,
            "decision_index": decision_index,
            "eligible_transition_count": len(eligible),
            "observed_balance_sats": (
                None if balances_sats is None
                else balances_sats.get(route["from"], 0)
            ),
            "fee_reserve_sats": fee_reserve_sats,
            "source_budget_remaining_sats": source_budget_remaining_sats,
        },
    }


EXPERIMENTAL_JOB_SOURCE = "seeded experimental generator"
ALLOCATION_COMMIT_JOB_SOURCE = "experimental allocation commit"
FINALIZATION_JOB_SOURCE = "experimental finalization"
FINALIZATION_CONSOLIDATION_JOB_SOURCE = (
    "experimental finalization consolidation"
)
TERMINAL_DISTRIBUTION_JOB_SOURCE = (
    "experimental terminal distribution"
)


def _generated_source(job):
    return json.loads(job["generated_from_json"] or "{}").get("source")


def experimental_workload_jobs(engine, flow_id):
    """Only seeded workload jobs count toward the approved decision total."""
    return [
        job for job in engine.list_jobs(flow_id)
        if _generated_source(job) == EXPERIMENTAL_JOB_SOURCE
    ]


def experimental_allocation_jobs(engine, flow_id):
    """Return the one-time reserve -> allocation-wallet commitment job."""
    return [
        job for job in engine.list_jobs(flow_id)
        if _generated_source(job) == ALLOCATION_COMMIT_JOB_SOURCE
    ]


def experimental_finalization_jobs(engine, flow_id):
    """Legacy worker -> destination finalization jobs."""
    return [
        job for job in engine.list_jobs(flow_id)
        if _generated_source(job) == FINALIZATION_JOB_SOURCE
    ]


def experimental_consolidation_jobs(engine, flow_id):
    """Multi-destination worker/hub -> finalization-wallet sweeps."""
    return [
        job for job in engine.list_jobs(flow_id)
        if _generated_source(job)
        == FINALIZATION_CONSOLIDATION_JOB_SOURCE
    ]


def experimental_terminal_distribution_jobs(engine, flow_id):
    """Multi-destination terminal distribution transaction jobs."""
    return [
        job for job in engine.list_jobs(flow_id)
        if _generated_source(job)
        == TERMINAL_DISTRIBUTION_JOB_SOURCE
    ]


def generate_experimental_allocation_job(engine, flow_id):
    """Commit the exact approved principal before randomized work begins.

    The reserve/source pays this transaction's network fee separately.
    The allocation wallet receives exactly initial_alloc_sats.
    """
    flow = engine.get_flow(flow_id)
    cfg = json.loads(engine.get_experiment(flow["experiment_id"])["config_json"])

    matches = [
        f for f in cfg["flows"]
        if f["source_wallet"] == flow["source_wallet"]
        and destination_identity(f) == flow["destination_wallet"]
    ]
    if len(matches) != 1:
        raise PlanError("cannot match this flow to exactly one flow in the approved config")

    allocation_wallet = matches[0].get("allocation_wallet")

    # Backward-compatible legacy experimental configs have no separate
    # commitment phase.
    if allocation_wallet is None:
        return None

    flow_wallets = json.loads(flow["flow_wallets_json"])
    if allocation_wallet not in flow_wallets:
        raise PlanError("approved allocation wallet is not a managed flow wallet")

    existing = experimental_allocation_jobs(engine, flow_id)
    if len(existing) > 1:
        raise PlanError("experimental flow has more than one allocation commitment")
    if existing:
        return None

    jobs = engine.list_jobs(flow_id)
    if jobs:
        raise PlanError("allocation commitment must be the first experimental job")

    return engine.add_job(
        flow_id,
        {
            "from": flow["source_wallet"],
            "to": allocation_wallet,
            "amount_sats": flow["initial_alloc_sats"],
            "step": 0,
        },
        planned_delay_s=0,
        generated_from={
            "source": ALLOCATION_COMMIT_JOB_SOURCE,
            "phase": "allocation",
            "allocation_wallet": allocation_wallet,
            "allocation_sats": flow["initial_alloc_sats"],
        },
    )


def generate_experimental_finalization_job(
        engine, flow_id, balances_sats, fee_reserve_sats=0):
    """Persist the next approved experimental finalization job.

    Legacy configs retain worker -> destination sweeps.

    Multi-destination configs first consolidate every non-finalizer
    workload wallet into finalization_wallet. Once those wallets are empty,
    one terminal distribution job is created from finalization_wallet.
    """
    if (
        not isinstance(fee_reserve_sats, int)
        or isinstance(fee_reserve_sats, bool)
        or fee_reserve_sats < 0
    ):
        raise PlanError(
            "fee_reserve_sats must be a non-negative integer"
        )

    flow = engine.get_flow(flow_id)
    cfg = json.loads(
        engine.get_experiment(flow["experiment_id"])["config_json"]
    )

    matches = [
        f for f in cfg["flows"]
        if f["source_wallet"] == flow["source_wallet"]
        and destination_identity(f) == flow["destination_wallet"]
    ]

    if len(matches) != 1:
        raise PlanError(
            "cannot match this flow to exactly one flow "
            "in the approved config"
        )

    approved_flow = matches[0]
    finalization = cfg.get("finalization") or {}

    jobs = engine.list_jobs(flow_id)

    if any(j["state"] != "CONFIRMED" for j in jobs):
        raise PlanError(
            "previous job must be confirmed before finalization"
        )

    workload = cfg.get("workload") or {}
    workload_jobs = experimental_workload_jobs(engine, flow_id)

    if (
        workload.get("mode") != "count"
        or len(workload_jobs) < workload.get("jobs", 0)
    ):
        raise PlanError(
            "experimental workload must complete before finalization"
        )

    workers = json.loads(flow["flow_wallets_json"])

    for wallet in workers:
        balance = balances_sats.get(wallet, 0)

        if (
            not isinstance(balance, int)
            or isinstance(balance, bool)
            or balance < 0
        ):
            raise PlanError(
                "finalization balances must be non-negative "
                "integer satoshis"
            )

    # ------------------------------------------------------
    # New multi-destination finalization.

    if has_multi_destinations(approved_flow):
        if (
            finalization.get("mode")
            != "consolidate_then_distribute"
        ):
            raise PlanError(
                "approved multi-destination finalization mode "
                "is not supported"
            )

        finalizer = approved_flow["finalization_wallet"]

        if finalizer not in workers:
            raise PlanError(
                "finalization wallet is not a managed flow wallet"
            )

        terminal_jobs = experimental_terminal_distribution_jobs(
            engine,
            flow_id,
        )

        if len(terminal_jobs) > 1:
            raise PlanError(
                "experimental flow has more than one terminal "
                "distribution job"
            )

        # Once the terminal distribution exists, there must never be
        # another consolidation or distribution plan.
        if terminal_jobs:
            return None

        consolidated_workers = {
            json.loads(job["planned_json"])["from"]
            for job in experimental_consolidation_jobs(
                engine,
                flow_id,
            )
        }

        # Sweep every non-finalizer workload wallet into the finalizer.
        # The finalizer itself is intentionally never swept to itself.
        for worker in workers:
            if worker == finalizer:
                continue

            if worker in consolidated_workers:
                continue

            balance = balances_sats.get(worker, 0)

            if balance == 0:
                continue

            if balance <= fee_reserve_sats:
                raise PlanError(
                    f"worker {worker} balance {balance} sats does not "
                    f"cover finalization fee reserve "
                    f"{fee_reserve_sats}"
                )

            return engine.add_job(
                flow_id,
                {
                    "from": worker,
                    "to": finalizer,
                    "amount_sats": "all",
                    "step": len(jobs),
                },
                planned_delay_s=0,
                generated_from={
                    "source":
                        FINALIZATION_CONSOLIDATION_JOB_SOURCE,
                    "phase": "consolidation",
                    "worker": worker,
                    "finalization_wallet": finalizer,
                    "balance_snapshot_sats":
                        dict(balances_sats),
                    "fee_reserve_sats": fee_reserve_sats,
                },
                depends_on=[jobs[-1]["id"]] if jobs else (),
            )

        # No non-finalizer wallet has a positive balance. The full
        # remaining experiment principal is now at finalization_wallet.
        balance = balances_sats.get(finalizer, 0)

        if balance <= 0:
            raise PlanError(
                f"finalization wallet {finalizer} has no balance "
                "to distribute"
            )

        if balance <= fee_reserve_sats:
            raise PlanError(
                f"finalization wallet {finalizer} balance "
                f"{balance} sats does not cover terminal distribution "
                f"fee reserve {fee_reserve_sats}"
            )

        return engine.add_job(
            flow_id,
            {
                "from": finalizer,
                "amount_sats": "all",
                "distribution": approved_flow["destinations"],
                "step": len(jobs),
            },
            planned_delay_s=0,
            generated_from={
                "source": TERMINAL_DISTRIBUTION_JOB_SOURCE,
                "phase": "terminal_distribution",
                "finalization_wallet": finalizer,
                "balance_snapshot_sats": dict(balances_sats),
                "fee_reserve_sats": fee_reserve_sats,
            },
            depends_on=[jobs[-1]["id"]] if jobs else (),
        )

    # ------------------------------------------------------
    # Legacy single-destination behavior.

    if (
        finalization.get("mode")
        != "sweep_workers_to_destination"
    ):
        raise PlanError(
            "approved experimental finalization mode "
            "is not supported"
        )

    finalized_workers = {
        json.loads(job["planned_json"])["from"]
        for job in experimental_finalization_jobs(
            engine,
            flow_id,
        )
    }

    for worker in workers:
        if worker in finalized_workers:
            continue

        balance = balances_sats.get(worker, 0)

        if balance == 0:
            continue

        if balance <= fee_reserve_sats:
            raise PlanError(
                f"worker {worker} balance {balance} sats does not cover "
                f"finalization fee reserve {fee_reserve_sats}"
            )

        return engine.add_job(
            flow_id,
            {
                "from": worker,
                "to": flow["destination_wallet"],
                "amount_sats": "all",
                "step": len(jobs),
            },
            planned_delay_s=0,
            generated_from={
                "source": FINALIZATION_JOB_SOURCE,
                "phase": "finalization",
                "worker": worker,
                "balance_snapshot_sats": dict(balances_sats),
                "fee_reserve_sats": fee_reserve_sats,
            },
            depends_on=[jobs[-1]["id"]] if jobs else (),
        )

    return None


def generate_experimental_job(
        engine, flow_id, balances_sats, fee_reserve_sats=0):
    """Generate and persist exactly one experimental job.

    The caller supplies the observed confirmed-balance snapshot. Previous
    experimental jobs must already be CONFIRMED before another is generated.
    """
    flow = engine.get_flow(flow_id)
    cfg = json.loads(engine.get_experiment(flow["experiment_id"])["config_json"])

    rnd = cfg.get("randomization", {"enabled": False})
    if not rnd.get("enabled"):
        raise PlanError("experimental job generation requires randomization to be enabled")

    workload = cfg.get("workload")
    if not workload:
        raise PlanError("experimental job generation requires a workload envelope")
    if workload["mode"] != "count":
        raise PlanError("generator v1 currently supports count workloads only")

    jobs = engine.list_jobs(flow_id)

    if any(j["state"] != "CONFIRMED" for j in jobs):
        raise PlanError("previous experimental job must be confirmed before generating the next")

    workload_jobs = experimental_workload_jobs(engine, flow_id)
    decision_index = len(workload_jobs)
    if decision_index >= workload["jobs"]:
        raise PlanError("experimental workload is already complete")

    source_used_sats = 0
    for job in workload_jobs:
        planned = json.loads(job["planned_json"])
        if planned["from"] == flow["source_wallet"]:
            amount = planned["amount_sats"]
            if not isinstance(amount, int) or isinstance(amount, bool):
                raise PlanError("experimental source job has a non-integer amount")
            source_used_sats += amount

    source_budget_remaining_sats = max(
        0,
        flow["initial_alloc_sats"] - source_used_sats,
    )

    decision = experimental_decision(
        engine,
        flow_id,
        decision_index,
        balances_sats=balances_sats,
        fee_reserve_sats=fee_reserve_sats,
        source_budget_remaining_sats=source_budget_remaining_sats,
    )

    generated_from = dict(decision["generated_from"])
    generated_from["phase"] = "workload"
    generated_from["balance_snapshot_sats"] = dict(balances_sats)
    generated_from["source_budget_used_sats"] = source_used_sats

    job_id = engine.add_job(
        flow_id,
        {
            "from": decision["from"],
            "to": decision["to"],
            "amount_sats": decision["amount_sats"],
            "step": len(jobs),
        },
        planned_delay_s=decision["delay_seconds"],
        generated_from=generated_from,
        depends_on=[jobs[-1]["id"]] if jobs else (),
    )

    return job_id


def settlement_cycle_decision(
        engine,
        flow_id,
        phase,
        decision_index,
        balances_sats,
        fee_reserve_sats=0,
):
    """Return one reproducible Settlement Cycle workload decision."""
    if phase not in ("workload", "return_workload"):
        raise PlanError(
            "Settlement Cycle decision phase must be "
            "workload or return_workload"
        )

    if (
        not isinstance(decision_index, int)
        or isinstance(decision_index, bool)
        or decision_index < 0
    ):
        raise PlanError(
            "decision_index must be a non-negative integer"
        )

    if (
        not isinstance(fee_reserve_sats, int)
        or isinstance(fee_reserve_sats, bool)
        or fee_reserve_sats < 0
    ):
        raise PlanError(
            "fee_reserve_sats must be a non-negative integer"
        )

    if not isinstance(balances_sats, dict):
        raise PlanError(
            "balances_sats must be a wallet -> integer sats mapping"
        )

    for wallet, value in balances_sats.items():
        if (
            not isinstance(wallet, str)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise PlanError(
                "balances_sats must contain non-negative "
                "integer satoshi balances"
            )

    flow = engine.get_flow(flow_id)
    cfg = json.loads(
        engine.get_experiment(flow["experiment_id"])["config_json"]
    )

    play = cfg.get("play") or {}
    if not (
        play.get("name") == "settlement_cycle"
        and play.get("version") == 1
    ):
        raise PlanError(
            "settlement_cycle_decision requires Settlement Cycle V1"
        )

    matches = [
        (i, f)
        for i, f in enumerate(cfg["flows"])
        if f["source_wallet"] == flow["source_wallet"]
        and destination_identity(f) == flow["destination_wallet"]
    ]

    if len(matches) != 1:
        raise PlanError(
            "cannot match this flow to exactly one flow "
            "in the approved config"
        )

    flow_index, _ = matches[0]

    rnd = cfg.get("randomization") or {}
    if not (
        rnd.get("enabled") is True
        and rnd.get("model") == "seeded_deterministic"
    ):
        raise PlanError(
            "Settlement Cycle requires seeded_deterministic "
            "randomization"
        )

    cycle = cfg["settlement_cycle"]

    if phase == "workload":
        spec_name = "outbound"
        source_name = "settlement cycle outbound"
    else:
        spec_name = "return"
        source_name = "settlement cycle return"

    spec = cycle[spec_name]
    minimum = spec["amount_sats_min"]

    material = (
        f"flowlab-settlement-v{GENERATOR_VERSION}:"
        f"{rnd['seed']}:{flow_index}:{phase}:{decision_index}"
    ).encode()

    derived_seed = int.from_bytes(
        hashlib.sha256(material).digest(),
        "big",
    )
    rng = random.Random(derived_seed)

    def spendable(transition):
        return (
            balances_sats.get(transition["from"], 0)
            - fee_reserve_sats
        )

    eligible = [
        transition
        for transition in spec["transitions"]
        if spendable(transition) >= minimum
    ]

    if not eligible:
        raise PlanError(
            "no approved Settlement Cycle transition currently "
            "has enough confirmed balance"
        )

    route = eligible[rng.randrange(len(eligible))]

    available = min(
        spec["amount_sats_max"],
        spendable(route),
    )

    if available < minimum:
        raise PlanError(
            "selected Settlement Cycle route cannot satisfy "
            "the approved minimum amount"
        )

    amount = rng.randint(
        minimum,
        available,
    )

    delay = rng.randint(
        spec["delay_seconds_min"],
        spec["delay_seconds_max"],
    )

    return {
        "from": route["from"],
        "to": route["to"],
        "amount_sats": amount,
        "delay_seconds": delay,
        "generated_from": {
            "source": source_name,
            "phase": phase,
            "generator_version": GENERATOR_VERSION,
            "model": rnd["model"],
            "seed": rnd["seed"],
            "flow_index": flow_index,
            "decision_index": decision_index,
            "eligible_transition_count": len(eligible),
            "observed_balance_sats":
                balances_sats.get(route["from"], 0),
            "fee_reserve_sats": fee_reserve_sats,
        },
    }


def generate_settlement_cycle_phase_job(
        engine,
        flow_id,
        balances_sats,
        fee_reserve_sats=0,
):
    """Generate one Settlement Cycle V1 lifecycle job."""
    if (
        not isinstance(fee_reserve_sats, int)
        or isinstance(fee_reserve_sats, bool)
        or fee_reserve_sats < 0
    ):
        raise PlanError(
            "fee_reserve_sats must be a non-negative integer"
        )

    flow = engine.get_flow(flow_id)
    cfg = json.loads(
        engine.get_experiment(flow["experiment_id"])["config_json"]
    )

    play = cfg.get("play") or {}
    if not (
        play.get("name") == "settlement_cycle"
        and play.get("version") == 1
    ):
        raise PlanError(
            "generate_settlement_cycle_phase_job requires "
            "Settlement Cycle V1"
        )

    cycle = cfg["settlement_cycle"]
    stage = cycle["settlement"]["source_wallet"]

    jobs = engine.list_jobs(flow_id)

    if any(job["state"] != "CONFIRMED" for job in jobs):
        raise PlanError(
            "previous Settlement Cycle job must be confirmed "
            "before generating the next"
        )

    progress = settlement_cycle_progress(
        engine,
        flow_id,
    )

    if progress["transaction_limit_reached"]:
        raise PlanError(
            "Settlement Cycle max_total_transactions reached"
        )

    phase = settlement_cycle_next_phase(
        engine,
        flow_id,
        balances_sats,
    )

    depends_on = [jobs[-1]["id"]] if jobs else ()

    if phase in ("workload", "return_workload"):
        if phase == "workload":
            decision_index = progress["outbound_decisions"]
        else:
            decision_index = progress["return_decisions"]

        decision = settlement_cycle_decision(
            engine,
            flow_id,
            phase,
            decision_index,
            balances_sats=balances_sats,
            fee_reserve_sats=fee_reserve_sats,
        )

        generated_from = dict(decision["generated_from"])
        generated_from["balance_snapshot_sats"] = dict(
            balances_sats
        )

        return engine.add_job(
            flow_id,
            {
                "from": decision["from"],
                "to": decision["to"],
                "amount_sats": decision["amount_sats"],
                "step": len(jobs),
            },
            planned_delay_s=decision["delay_seconds"],
            generated_from=generated_from,
            depends_on=depends_on,
        )

    if phase == "settlement":
        spec = cycle["settlement"]

        material = (
            f"flowlab-settlement-delay-v{GENERATOR_VERSION}:"
            f"{cfg['randomization']['seed']}:"
            f"{flow_id}:settlement"
        ).encode()

        derived_seed = int.from_bytes(
            hashlib.sha256(material).digest(),
            "big",
        )
        rng = random.Random(derived_seed)

        delay = rng.randint(
            spec["delay_seconds_min"],
            spec["delay_seconds_max"],
        )

        return engine.add_job(
            flow_id,
            {
                "from": spec["source_wallet"],
                "amount_sats": "all",
                "settlement": spec,
                "step": len(jobs),
            },
            planned_delay_s=delay,
            generated_from={
                "source": "settlement cycle settlement",
                "phase": "settlement",
                "generator_version": GENERATOR_VERSION,
                "model": cfg["randomization"]["model"],
                "seed": cfg["randomization"]["seed"],
                "balance_snapshot_sats": dict(balances_sats),
            },
            depends_on=depends_on,
        )

    if phase == "consolidation":
        internal_wallets = list(dict.fromkeys([
            *cycle["hubs"],
            *cycle["workers"],
        ]))

        for wallet in internal_wallets:
            balance = balances_sats.get(wallet, 0)

            if (
                not isinstance(balance, int)
                or isinstance(balance, bool)
                or balance < 0
            ):
                raise PlanError(
                    "Settlement Cycle consolidation balances must be "
                    "non-negative integer satoshis"
                )

            if balance == 0:
                continue

            if balance <= fee_reserve_sats:
                raise PlanError(
                    f"wallet {wallet} balance {balance} sats does not "
                    f"cover consolidation fee reserve "
                    f"{fee_reserve_sats}"
                )

            return engine.add_job(
                flow_id,
                {
                    "from": wallet,
                    "to": stage,
                    "amount_sats": "all",
                    "step": len(jobs),
                },
                planned_delay_s=0,
                generated_from={
                    "source": "settlement cycle consolidation",
                    "phase": "consolidation",
                    "wallet": wallet,
                    "stage_wallet": stage,
                    "balance_snapshot_sats":
                        dict(balances_sats),
                    "fee_reserve_sats":
                        fee_reserve_sats,
                },
                depends_on=depends_on,
            )

        raise PlanError(
            "Settlement Cycle consolidation requested with no "
            "positive internal wallet balance"
        )

    if phase == "reserve_return":
        spec = cycle["reserve_return"]
        stage_balance = balances_sats.get(spec["from_wallet"], 0)

        if (
            not isinstance(stage_balance, int)
            or isinstance(stage_balance, bool)
            or stage_balance < 0
        ):
            raise PlanError(
                "Settlement Cycle Stage balance must be a "
                "non-negative integer number of satoshis"
            )

        if stage_balance <= fee_reserve_sats:
            raise PlanError(
                f"Stage balance {stage_balance} sats does not cover "
                f"reserve-return fee reserve {fee_reserve_sats}"
            )

        material = (
            f"flowlab-settlement-delay-v{GENERATOR_VERSION}:"
            f"{cfg['randomization']['seed']}:"
            f"{flow_id}:reserve_return"
        ).encode()

        derived_seed = int.from_bytes(
            hashlib.sha256(material).digest(),
            "big",
        )
        rng = random.Random(derived_seed)

        delay = rng.randint(
            spec["delay_seconds_min"],
            spec["delay_seconds_max"],
        )

        return engine.add_job(
            flow_id,
            {
                "from": spec["from_wallet"],
                "to": spec["to_wallet"],
                "amount_sats": "all",
                "step": len(jobs),
            },
            planned_delay_s=delay,
            generated_from={
                "source": "settlement cycle reserve return",
                "phase": "reserve_return",
                "generator_version": GENERATOR_VERSION,
                "model": cfg["randomization"]["model"],
                "seed": cfg["randomization"]["seed"],
                "stage_wallet": spec["from_wallet"],
                "reserve_wallet": spec["to_wallet"],
                "balance_snapshot_sats":
                    dict(balances_sats),
                "fee_reserve_sats":
                    fee_reserve_sats,
            },
            depends_on=depends_on,
        )

    raise PlanError(
        f"Settlement Cycle phase {phase!r} does not yet have "
        "a job generator"
    )


def settlement_cycle_next_phase(engine, flow_id, balances_sats):
    """Return the next Settlement Cycle V1 lifecycle phase."""
    flow = engine.get_flow(flow_id)
    cfg = json.loads(
        engine.get_experiment(flow["experiment_id"])["config_json"]
    )

    play = cfg.get("play") or {}
    if not (
        play.get("name") == "settlement_cycle"
        and play.get("version") == 1
    ):
        raise PlanError(
            "settlement_cycle_next_phase requires Settlement Cycle V1"
        )

    cycle = cfg.get("settlement_cycle")
    if not isinstance(cycle, dict):
        raise PlanError(
            "approved Settlement Cycle config is missing settlement_cycle"
        )

    if not isinstance(balances_sats, dict):
        raise PlanError(
            "Settlement Cycle balances must be a wallet -> sats mapping"
        )

    for wallet, value in balances_sats.items():
        if (
            not isinstance(wallet, str)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise PlanError(
                "Settlement Cycle balances must contain "
                "non-negative integer satoshi balances"
            )

    jobs = engine.list_jobs(flow_id)

    def phase_jobs(phase):
        result = []
        for job in jobs:
            generated = json.loads(
                job["generated_from_json"] or "{}"
            )
            if generated.get("phase") == phase:
                result.append(job)
        return result

    # Never plan past an unfinished persisted transaction.
    unfinished = [
        job for job in jobs
        if job["state"] != "CONFIRMED"
    ]
    if unfinished:
        generated = json.loads(
            unfinished[0]["generated_from_json"] or "{}"
        )
        phase = generated.get("phase")
        if not isinstance(phase, str) or not phase:
            raise PlanError(
                "unfinished Settlement Cycle job has no lifecycle phase"
            )
        return phase

    allocation_jobs = phase_jobs("allocation")
    if not allocation_jobs:
        return "allocation"

    progress = settlement_cycle_progress(
        engine,
        flow_id,
    )

    if progress["outbound_decisions"] < cycle["outbound"]["decisions"]:
        if progress["transaction_limit_reached"]:
            raise PlanError(
                "Settlement Cycle max_total_transactions reached "
                "before outbound workload completed"
            )
        return "workload"

    settlement_jobs = phase_jobs("settlement")

    internal_wallets = list(dict.fromkeys([
        *cycle["hubs"],
        *cycle["workers"],
    ]))

    internal_balance = sum(
        balances_sats.get(wallet, 0)
        for wallet in internal_wallets
    )

    # Before the intermediate settlement, every worker/hub balance
    # must first return to Stage.
    if not settlement_jobs:
        if internal_balance > 0:
            if progress["transaction_limit_reached"]:
                raise PlanError(
                    "Settlement Cycle max_total_transactions reached "
                    "before pre-settlement consolidation completed"
                )
            return "consolidation"

        if progress["transaction_limit_reached"]:
            raise PlanError(
                "Settlement Cycle max_total_transactions reached "
                "before settlement"
            )
        return "settlement"

    if progress["return_decisions"] < cycle["return"]["decisions"]:
        if progress["transaction_limit_reached"]:
            raise PlanError(
                "Settlement Cycle max_total_transactions reached "
                "before return workload completed"
            )
        return "return_workload"

    # After the return workload, consolidate again before returning
    # the retained experiment principal to Reserve.
    if internal_balance > 0:
        if progress["transaction_limit_reached"]:
            raise PlanError(
                "Settlement Cycle max_total_transactions reached "
                "before return consolidation completed"
            )
        return "consolidation"

    reserve_return_jobs = phase_jobs("reserve_return")
    if reserve_return_jobs:
        return "complete"

    if progress["transaction_limit_reached"]:
        raise PlanError(
            "Settlement Cycle max_total_transactions reached "
            "before reserve return"
        )

    return "reserve_return"


def settlement_cycle_progress(engine, flow_id):
    """Return deterministic persisted progress for Settlement Cycle V1."""
    flow = engine.get_flow(flow_id)
    cfg = json.loads(
        engine.get_experiment(flow["experiment_id"])["config_json"]
    )

    play = cfg.get("play") or {}
    if not (
        play.get("name") == "settlement_cycle"
        and play.get("version") == 1
    ):
        raise PlanError(
            "settlement_cycle_progress requires Settlement Cycle V1"
        )

    cycle = cfg.get("settlement_cycle")
    if not isinstance(cycle, dict):
        raise PlanError(
            "approved Settlement Cycle config is missing settlement_cycle"
        )

    jobs = engine.list_jobs(flow_id)

    outbound = 0
    returned = 0

    for job in jobs:
        generated = json.loads(
            job["generated_from_json"] or "{}"
        )
        phase = generated.get("phase")

        if phase == "workload":
            outbound += 1
        elif phase == "return_workload":
            returned += 1

    maximum = cycle["max_total_transactions"]

    return {
        "outbound_decisions": outbound,
        "return_decisions": returned,
        "total_transactions": len(jobs),
        "all_jobs_confirmed": all(
            job["state"] == "CONFIRMED"
            for job in jobs
        ),
        "max_total_transactions": maximum,
        "transaction_limit_reached": len(jobs) >= maximum,
    }


def generate_jobs(engine, flow_id, play_context=None):
    """Create one job per configured transfer, in order. Returns the job ids."""
    if engine.list_jobs(flow_id):
        raise PlanError("this flow already has jobs; refusing to plan it twice")

    flow = engine.get_flow(flow_id)
    cfg = json.loads(
        engine.get_experiment(flow["experiment_id"])["config_json"]
    )
    play = cfg.get("play") or {}

    transfers = transfers_for(engine, flow_id)

    fan_out_fan_in = (
        play.get("name") == "fan_out_fan_in"
        and play.get("version") == 1
    )

    if fan_out_fan_in:
        if len(transfers) < 6 or (len(transfers) - 2) % 2 != 0:
            raise PlanError(
                "Fan-Out/Fan-In V1 has an invalid transfer lifecycle"
            )
        branch_count = (len(transfers) - 2) // 2
    else:
        branch_count = 0

    ids = []
    for i, t in enumerate(transfers):
        phase = "workload"

        if fan_out_fan_in:
            if i == 0:
                phase = "allocation"
            elif i == len(transfers) - 1:
                phase = "finalization"
            elif i <= branch_count:
                phase = "workload"
            else:
                phase = "consolidation"

        generated_from = {
            "step": i,
            "source": "approved config",
            "phase": phase,
        }

        if (
            fan_out_fan_in
            and phase == "finalization"
        ):
            if not isinstance(play_context, dict):
                raise PlanError(
                    "Fan-Out/Fan-In V1 requires persisted Play context"
                )

            baseline = play_context.get("stage_baseline_sats")

            if (
                not isinstance(baseline, int)
                or isinstance(baseline, bool)
                or baseline < 0
            ):
                raise PlanError(
                    "Fan-Out/Fan-In V1 has an invalid Stage baseline"
                )

            generated_from["stage_baseline_sats"] = baseline
            generated_from["stage_wallet"] = play_context.get(
                "stage_wallet"
            )

        ids.append(engine.add_job(
            flow_id,
            {
                "from": t["from"],
                "to": t["to"],
                "amount_sats": t["amount_sats"],
                "step": i,
            },
            planned_delay_s=t["delay_seconds"],
            generated_from=generated_from,
            depends_on=[ids[-1]] if ids else (),
        ))
    return ids


def next_due(engine, flow_id):
    """Strictly one job at a time, in order. Returns {"job", "wait_s", "reason"}."""
    jobs = engine.list_jobs(flow_id)
    now = datetime.fromisoformat(engine.now())
    prev = None
    for j in jobs:
        if j["state"] == "CONFIRMED":
            prev = j
            continue
        if j["state"] != "PLANNED":
            return {"job": None, "wait_s": None, "reason": f"job {j['seq']} is {j['state']}"}
        base = datetime.fromisoformat(prev["updated_at"] if prev else j["created_at"])
        ready = base + timedelta(seconds=j["planned_delay_s"] or 0)
        wait = max(0, int((ready - now).total_seconds() + 0.999))
        if wait > 0:
            return {"job": None, "wait_s": wait, "reason": f"job {j['seq']} not due yet"}
        return {"job": j, "wait_s": 0, "reason": "due"}
    return {"job": None, "wait_s": None, "reason": "all jobs confirmed"}
