"""Mixer Lab verified-deposit accounting simulation.

This module does not authorize live financial crediting.
Simulation must be explicitly requested by the caller.
"""

import sqlite3

from . import pool_ledger
from .pool_verifier import verify_observation


DEFAULT_CONFIRMATIONS = 6


def simulate_credit(
    conn, rpc, *, observation_id, min_confirmations=DEFAULT_CONFIRMATIONS
):
    """Evaluate a verified deposit without changing accounting records."""

    if type(observation_id) is not int or observation_id <= 0:
        raise ValueError("Invalid observation ID")

    if (
        type(min_confirmations) is not int
        or not 1 <= min_confirmations <= 1000
    ):
        raise ValueError("Invalid confirmation threshold")

    observation = conn.execute(
        """
        SELECT o.pool_id, o.session_id, o.txid, o.vout,
               o.amount_sats, s.fee_basis_points
        FROM pool_deposit_observations o
        JOIN pool_sessions s
          ON s.id = o.session_id AND s.pool_id = o.pool_id
        WHERE o.id = ?
        """,
        (observation_id,),
    ).fetchone()

    if observation is None:
        raise ValueError("Unknown observation")

    existing = conn.execute(
        """
        SELECT id, pool_id, session_id, amount_sats
        FROM pool_deposits
        WHERE txid = ? AND vout = ?
        """,
        (observation["txid"], observation["vout"]),
    ).fetchone()

    if existing is not None:
        if (
            existing["pool_id"] != observation["pool_id"]
            or existing["session_id"] != observation["session_id"]
            or existing["amount_sats"] != observation["amount_sats"]
        ):
            return {
                "state": "CREDIT_CONFLICT",
                "reason": "Credited outpoint does not match observation",
            }

        return {
            "state": "ALREADY_CREDITED",
            "deposit_id": existing["id"],
        }

    verification = verify_observation(
        conn,
        rpc,
        observation_id=observation_id,
    )

    if verification["state"] != "VERIFIED":
        return {
            "state": "NOT_ELIGIBLE",
            "reason": verification["reason"],
        }

    if verification["confirmations"] < min_confirmations:
        return {
            "state": "INSUFFICIENT_CONFIRMATIONS",
            "confirmations": verification["confirmations"],
            "required": min_confirmations,
        }

    previous = pool_ledger.session_balances(
        conn,
        pool_id=observation["pool_id"],
        session_id=observation["session_id"],
    )

    amount = observation["amount_sats"]
    bps = observation["fee_basis_points"]

    # Match the integer boundary enforced by record_deposit().
    if (
        previous['deposit_sats'] < 0
        or amount <= 0
        or amount > pool_ledger.MAX_SQLITE_INTEGER
        - previous['deposit_sats']
    ):
        raise ValueError(
            "Cumulative session deposits exceed integer limit"
        )

    previous_fee = (
        previous["deposit_sats"] * bps + 5_000
    ) // 10_000

    next_fee = (
        (previous["deposit_sats"] + amount) * bps + 5_000
    ) // 10_000

    incremental_fee = next_fee - previous_fee

    return {
        "state": "ELIGIBLE_SIMULATION",
        "pool_id": observation["pool_id"],
        "session_id": observation["session_id"],
        "amount_sats": amount,
        "fee_sats": incremental_fee,
        "participant_liability_sats": amount - incremental_fee,
        "confirmations": verification["confirmations"],
        "required": min_confirmations,
    }


def credit_live(*args, **kwargs):
    """Live posting is deliberately unavailable."""

    raise RuntimeError(
        "Live deposit crediting is disabled pending "
        "post-credit reorganization monitoring and operator approval"
    )
