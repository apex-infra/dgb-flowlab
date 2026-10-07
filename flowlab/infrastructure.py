"""
Read-only FlowLab infrastructure / wallet / UTXO analysis.

This module never mutates DigiByte Core. It observes only configured FlowLab
wallets and returns a browser-safe, versioned research snapshot.
"""

from __future__ import annotations

import statistics

from .rpc import to_sats


ROLE_ORDER = (
    "reserve",
    "stage",
    "workers",
    "hubs",
    "destinations",
)

SCORE_WEIGHTS = {
    "wallet_availability": 0.15,
    "role_coverage": 0.10,
    "node_readiness": 0.10,
    "confirmed_utxo_ratio": 0.10,
    "balance_dispersion": 0.20,
    "utxo_dispersion": 0.20,
    "utxo_size_diversity": 0.15,
}

WALLET_SCORE_WEIGHTS = {
    "confirmed_ratio": 0.25,
    "utxo_size_diversity": 0.25,
    "utxo_evenness": 0.25,
    "fragmentation_index": 0.25,
}


def _safe_sats(value):
    if value is None:
        return 0
    return to_sats(value)


def _stats(values):
    vals = [int(v) for v in values]

    if not vals:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "stdev": None,
        }

    return {
        "count": len(vals),
        "min": min(vals),
        "max": max(vals),
        "mean": statistics.mean(vals),
        "median": statistics.median(vals),
        "stdev": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
    }


def _inverse_concentration(values):
    """
    Normalized inverse HHI.

    0.0 means all observed mass is in one category.
    1.0 means the observed mass is evenly distributed across categories.

    This is descriptive only; neither end is universally preferable.
    """
    vals = [float(v) for v in values if v > 0]

    if len(vals) <= 1:
        return 0.0

    total = sum(vals)
    shares = [v / total for v in vals]
    hhi = sum(s * s for s in shares)

    minimum_hhi = 1.0 / len(vals)
    maximum_hhi = 1.0

    return max(
        0.0,
        min(
            1.0,
            (maximum_hhi - hhi) /
            (maximum_hhi - minimum_hhi),
        ),
    )


def _bounded_variation(values):
    vals = [float(v) for v in values if v >= 0]

    if len(vals) < 2:
        return 0.0

    mean = statistics.mean(vals)

    if mean == 0:
        return 0.0

    cv = statistics.pstdev(vals) / mean
    return cv / (1.0 + cv)


def _role_map(wallet_roles):
    out = {}

    for role in ROLE_ORDER:
        for wallet in (wallet_roles or {}).get(role, ()):
            out.setdefault(wallet, role.rstrip("s"))

    return out


def _competition_ranks(values):
    """1-based competition ranking, with equal values sharing a rank."""
    ordered = sorted(
        ((name, value) for name, value in values.items()),
        key=lambda item: (-item[1], item[0]),
    )

    ranks = {}
    prior_value = None
    prior_rank = None

    for position, (name, value) in enumerate(ordered, start=1):
        if prior_value is not None and value == prior_value:
            rank = prior_rank
        else:
            rank = position

        ranks[name] = rank
        prior_value = value
        prior_rank = rank

    return ranks


def _score_label(score):
    if score >= 85:
        return "VERY HIGH"
    if score >= 70:
        return "HIGH"
    if score >= 50:
        return "MODERATE"
    if score >= 25:
        return "LIMITED"
    return "LOW"


def _safe_utxo(raw, confirmed_keys):
    txid = raw.get("txid")
    vout = raw.get("vout")
    key = (txid, vout)

    amount_sats = _safe_sats(raw.get("amount", 0))

    return {
        "txid": txid,
        "vout": vout,
        "amount_sats": amount_sats,
        "confirmed": key in confirmed_keys,
        "confirmations": raw.get("confirmations"),
        "address": raw.get("address"),
        "spendable": raw.get("spendable"),
        "solvable": raw.get("solvable"),
        "safe": raw.get("safe"),
    }


def collect_infrastructure(rpc, wallets, wallet_roles=None):
    """
    Inspect the configured FlowLab infrastructure.

    Reads:
      * listwallets
      * getblockchaininfo
      * getbalances per loaded configured wallet
      * listunspent(minconf=0) per loaded configured wallet
      * listunspent(minconf=1) per loaded configured wallet

    No RPC mutation is performed.
    """
    wallets = list(wallets or ())
    roles = _role_map(wallet_roles)

    try:
        loaded = set(rpc.list_wallets())
    except Exception:  # noqa: BLE001
        loaded = set()

    try:
        chain = rpc.get_blockchain_info() or {}
        blocks = chain.get("blocks")
        headers = chain.get("headers")
        ibd = bool(chain.get("initialblockdownload"))

        node_readiness = (
            1.0
            if (
                not ibd
                and isinstance(blocks, int)
                and isinstance(headers, int)
                and blocks >= headers
            )
            else 0.0
        )

        node = {
            "available": True,
            "blocks": blocks,
            "headers": headers,
            "initial_block_download": ibd,
            "ready": bool(node_readiness),
        }
    except Exception:  # noqa: BLE001
        node_readiness = 0.0
        node = {
            "available": False,
            "blocks": None,
            "headers": None,
            "initial_block_download": None,
            "ready": False,
        }

    wallet_data = {}
    all_utxo_sizes = []

    trusted_total = 0
    pending_total = 0
    immature_total = 0
    confirmed_utxo_count = 0
    unconfirmed_utxo_count = 0

    available_count = 0

    for wallet in wallets:
        role = roles.get(wallet)

        base = {
            "name": wallet,
            "role": role,
            "loaded": wallet in loaded,
            "available": False,
            "trusted_balance_sats": None,
            "pending_balance_sats": None,
            "immature_balance_sats": None,
            "total_balance_sats": None,
            "utxo_count": None,
            "confirmed_utxo_count": None,
            "unconfirmed_utxo_count": None,
            "utxo_stats": _stats([]),
            "utxos": [],
        }

        if wallet not in loaded:
            wallet_data[wallet] = base
            continue

        try:
            balances = rpc.get_balances(wallet) or {}
            mine = balances.get("mine") or {}

            trusted = _safe_sats(mine.get("trusted", 0))
            pending = _safe_sats(mine.get("untrusted_pending", 0))
            immature = _safe_sats(mine.get("immature", 0))

            all_unspent = list(rpc.list_unspent(wallet, 0) or ())
            confirmed = list(rpc.list_unspent(wallet, 1) or ())

            confirmed_keys = {
                (u.get("txid"), u.get("vout"))
                for u in confirmed
            }

            utxos = [
                _safe_utxo(u, confirmed_keys)
                for u in all_unspent
            ]

            sizes = [u["amount_sats"] for u in utxos]
            confirmed_count = sum(u["confirmed"] for u in utxos)
            unconfirmed_count = len(utxos) - confirmed_count

            base.update({
                "available": True,
                "trusted_balance_sats": trusted,
                "pending_balance_sats": pending,
                "immature_balance_sats": immature,
                "total_balance_sats": trusted + pending + immature,
                "utxo_count": len(utxos),
                "confirmed_utxo_count": confirmed_count,
                "unconfirmed_utxo_count": unconfirmed_count,
                "utxo_stats": _stats(sizes),
                "utxos": utxos,
            })

            available_count += 1
            trusted_total += trusted
            pending_total += pending
            immature_total += immature
            confirmed_utxo_count += confirmed_count
            unconfirmed_utxo_count += unconfirmed_count
            all_utxo_sizes.extend(sizes)

        except Exception:  # noqa: BLE001
            pass

        wallet_data[wallet] = base

    available_wallets = [
        wallet
        for wallet in wallets
        if wallet_data[wallet]["available"]
    ]

    unavailable_wallets = [
        wallet
        for wallet in wallets
        if not wallet_data[wallet]["available"]
    ]

    wallet_count = len(wallets)
    utxo_count = confirmed_utxo_count + unconfirmed_utxo_count

    wallet_availability = (
        available_count / wallet_count
        if wallet_count
        else 0.0
    )

    assigned_count = sum(wallet in roles for wallet in wallets)
    role_coverage = (
        assigned_count / wallet_count
        if wallet_count
        else 0.0
    )

    confirmed_utxo_ratio = (
        confirmed_utxo_count / utxo_count
        if utxo_count
        else 0.0
    )

    balances_for_dispersion = [
        wallet_data[w]["total_balance_sats"] or 0
        for w in available_wallets
    ]

    utxos_for_dispersion = [
        wallet_data[w]["utxo_count"] or 0
        for w in available_wallets
    ]

    balance_dispersion = _inverse_concentration(
        balances_for_dispersion
    )

    utxo_dispersion = _inverse_concentration(
        utxos_for_dispersion
    )

    utxo_size_diversity = _bounded_variation(
        all_utxo_sizes
    )

    components = {
        "wallet_availability": wallet_availability,
        "role_coverage": role_coverage,
        "node_readiness": node_readiness,
        "confirmed_utxo_ratio": confirmed_utxo_ratio,
        "balance_dispersion": balance_dispersion,
        "utxo_dispersion": utxo_dispersion,
        "utxo_size_diversity": utxo_size_diversity,
    }

    score = round(
        100.0 * sum(
            components[name] * weight
            for name, weight in SCORE_WEIGHTS.items()
        )
    )

    balance_values = {
        name: (
            wallet_data[name]["total_balance_sats"]
            if wallet_data[name]["available"]
            else 0
        ) or 0
        for name in wallets
    }

    utxo_count_values = {
        name: (
            wallet_data[name]["utxo_count"]
            if wallet_data[name]["available"]
            else 0
        ) or 0
        for name in wallets
    }

    balance_ranks = _competition_ranks(balance_values)
    utxo_count_ranks = _competition_ranks(utxo_count_values)

    max_utxo_count = max(utxo_count_values.values(), default=0)
    managed_total = (
        trusted_total
        + pending_total
        + immature_total
    )

    for name in wallets:
        wallet = wallet_data[name]
        count = wallet["utxo_count"] or 0
        total = wallet["total_balance_sats"] or 0
        utxos = wallet["utxos"]

        balance_share = (
            total / managed_total
            if managed_total
            else 0.0
        )

        utxo_share = (
            count / utxo_count
            if utxo_count
            else 0.0
        )

        if not wallet["available"] or not count:
            wallet["research"] = {
                "score": None,
                "label": "NO UTXO DATA",
                "balance_share": balance_share,
                "utxo_share": utxo_share,
                "balance_rank": balance_ranks[name],
                "utxo_count_rank": utxo_count_ranks[name],
                "confirmed_ratio": None,
                "utxo_size_diversity": None,
                "utxo_evenness": None,
                "fragmentation_index": None,
                "components": None,
                "weights": dict(WALLET_SCORE_WEIGHTS),
            }
            continue

        sizes = [u["amount_sats"] for u in utxos]

        confirmed_ratio = (
            wallet["confirmed_utxo_count"] / count
            if count
            else 0.0
        )

        size_diversity = _bounded_variation(sizes)
        utxo_evenness = _inverse_concentration(sizes)

        fragmentation_index = (
            count / max_utxo_count
            if max_utxo_count
            else 0.0
        )

        wallet_components = {
            "confirmed_ratio": confirmed_ratio,
            "utxo_size_diversity": size_diversity,
            "utxo_evenness": utxo_evenness,
            "fragmentation_index": fragmentation_index,
        }

        wallet_score = round(
            100.0 * sum(
                wallet_components[key] * weight
                for key, weight in WALLET_SCORE_WEIGHTS.items()
            )
        )

        wallet["research"] = {
            "score": wallet_score,
            "label": _score_label(wallet_score),
            "balance_share": balance_share,
            "utxo_share": utxo_share,
            "balance_rank": balance_ranks[name],
            "utxo_count_rank": utxo_count_ranks[name],
            "confirmed_ratio": confirmed_ratio,
            "utxo_size_diversity": size_diversity,
            "utxo_evenness": utxo_evenness,
            "fragmentation_index": fragmentation_index,
            "components": wallet_components,
            "weights": dict(WALLET_SCORE_WEIGHTS),
        }

    overall_stats = _stats(all_utxo_sizes)

    return {
        "model": "infrastructure_snapshot_v1",
        "overview": {
            "wallet_count": wallet_count,
            "available_wallet_count": available_count,
            "unavailable_wallet_count": len(unavailable_wallets),
            "available_wallets": available_wallets,
            "unavailable_wallets": unavailable_wallets,
            "managed_balance_sats": (
                trusted_total
                + pending_total
                + immature_total
            ),
            "trusted_balance_sats": trusted_total,
            "pending_balance_sats": pending_total,
            "immature_balance_sats": immature_total,
            "utxo_count": utxo_count,
            "confirmed_utxo_count": confirmed_utxo_count,
            "unconfirmed_utxo_count": unconfirmed_utxo_count,
            "utxo_stats": overall_stats,
        },
        "node": node,
        "wallets": wallet_data,
        "research": {
            "balance_concentration": (
                1.0 - balance_dispersion
            ),
            "balance_dispersion": balance_dispersion,
            "utxo_concentration": (
                1.0 - utxo_dispersion
            ),
            "utxo_dispersion": utxo_dispersion,
            "utxo_size_diversity": utxo_size_diversity,
            "confirmed_utxo_ratio": confirmed_utxo_ratio,
            "wallet_availability": wallet_availability,
            "role_coverage": role_coverage,
        },
        "score": {
            "model": "infrastructure_model_v1",
            "score": score,
            "label": _score_label(score),
            "components": components,
            "weights": dict(SCORE_WEIGHTS),
            "interpretation": (
                "Versioned FlowLab infrastructure measurement derived "
                "from wallet availability, role coverage, node readiness, "
                "confirmation state, and observed balance/UTXO distribution. "
                "The structural measurements are descriptive and do not "
                "declare any particular UTXO shape inherently preferable."
            ),
        },
    }
