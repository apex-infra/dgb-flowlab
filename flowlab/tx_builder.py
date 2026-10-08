"""
tx_builder.py -- build, verify, preview, then broadcast (as separate steps).

* build() never broadcasts. It picks the inputs itself, creates the raw
  transaction, funds it with add_inputs=False, signs it, then independently
  re-checks the decoded result: the destination must be one of OUR wallets,
  the outputs must be exactly (destination, change), the inputs must be the
  ones chosen, the fee must be under the cap, and the node must say it would
  accept it into the mempool.
* broadcast() is the only code that calls sendrawtransaction. It always goes
  through the engine's write-ahead journal, so an interrupted broadcast can
  never be retried automatically into a duplicate.
"""

import hashlib
from dataclasses import dataclass
from math import ceil

from .rpc import RpcError, to_sats
from .utxo_policy import UtxoPolicyError, select_utxos


class BuildError(Exception):
    pass


@dataclass(frozen=True)
class Prepared:
    source_wallet: str
    address: str
    amount_sats: int
    change_address: str
    hex: str
    txid: str
    fee_sats: int
    inputs: tuple
    outputs: tuple
    utxo_selection: dict | None = None

    def summary(self):
        lines = [f"from wallet : {self.source_wallet}",
                 f"to address  : {self.address}",
                 f"amount      : {self.amount_sats} sats",
                 f"fee         : {self.fee_sats} sats",
                 f"txid        : {self.txid}",
                 f"inputs      : {len(self.inputs)}"]
        for addr, sats in self.outputs:
            lines.append(f"output      : {sats} sats -> {addr}")
        return "\n".join(lines)


@dataclass(frozen=True)
class PreparedDistribution:
    source_wallet: str
    budget_sats: int
    distributed_sats: int
    change_address: str
    hex: str
    txid: str
    fee_sats: int
    inputs: tuple
    outputs: tuple
    destinations: tuple
    utxo_selection: dict | None = None

    def summary(self):
        lines = [
            f"from wallet : {self.source_wallet}",
            f"budget      : {self.budget_sats} sats",
            f"distributed : {self.distributed_sats} sats",
            f"fee         : {self.fee_sats} sats",
            f"txid        : {self.txid}",
            f"inputs      : {len(self.inputs)}",
        ]
        for addr, sats in self.destinations:
            lines.append(f"destination : {sats} sats -> {addr}")
        return "\n".join(lines)


class TxBuilder:
    def __init__(self, rpc, wallets, max_fee_sats=10_000_000):
        self.rpc = rpc
        self.wallets = list(wallets)
        self.max_fee_sats = int(max_fee_sats)

    def _smart_fee_rate_sat_vb(self, blocks=6):
        """Return the node's smart fee estimate in sat/vB.

        estimatesmartfee reports DGB/kvB. Convert that exact rate to sat/vB.
        If the node cannot provide a usable estimate, return None.
        """
        try:
            estimate = self.rpc.estimate_smart_fee(blocks)
            feerate = estimate.get("feerate")
            if feerate is None:
                return None
            sats_per_kvb = to_sats(feerate)
            if sats_per_kvb <= 0:
                return None
            return sats_per_kvb / 1000
        except Exception:  # noqa: BLE001
            return None

    def _fee_reserve(self, n_inputs):
        """Sats to keep back for the fee using the same smart-fee source that
        transaction funding uses. If the node cannot provide an estimate, fall
        back to the full configured fee cap."""
        rate_sat_vb = self._smart_fee_rate_sat_vb()
        if rate_sat_vb is None:
            return self.max_fee_sats
        size = 10 + 68 * n_inputs + 2 * 34
        return min(self.max_fee_sats, ceil(rate_sat_vb * size))

    def planning_fee_reserve_sats(self, n_inputs=8):
        """Conservative fee reserve used during planning.

        This is not the transaction's actual fee. It estimates relay-fee
        requirements for a moderately fragmented transaction so the planner
        does not reserve the entire max-fee safety ceiling.
        """
        if (not isinstance(n_inputs, int) or isinstance(n_inputs, bool)
                or n_inputs < 1):
            raise BuildError("planning fee input count must be a positive integer")
        return self._fee_reserve(n_inputs)

    def _policy_select_inputs(
            self,
            source_wallet,
            amount_sats,
            *,
            sweep,
            utxo_policy,
            phase,
            cohort_outpoints,
            seed_material,
            reserve_fee=True,
    ):
        """Apply an approved UTXO policy to the source wallet.

        TxBuilder deliberately knows nothing about experiments, databases,
        Plays, or cohort persistence. The caller supplies only the approved
        policy context and, when applicable, the already-persisted outpoints.
        """
        coins = self.rpc.list_unspent(
            source_wallet,
            0,
        )

        try:
            result = select_utxos(
                coins,
                utxo_policy,
                required_sats=None if sweep else amount_sats,
                sweep=sweep,
                phase=phase,
                cohort_outpoints=cohort_outpoints,
                seed_material=seed_material,
                fee_reserve_for_n=(
                    self._fee_reserve
                    if reserve_fee
                    else (lambda _n: 0)
                ),
            )
        except UtxoPolicyError as exc:
            message = str(exc)

            if "cannot satisfy requested amount" in message:
                raise BuildError(
                    "insufficient funds under approved UTXO policy"
                ) from exc

            raise BuildError(
                f"UTXO policy selection failed: {message}"
            ) from exc

        telemetry = result.telemetry()
        telemetry["selected_outpoints"] = [
            [u["txid"], u["vout"]]
            for u in result.selected
        ]

        return list(result.selected), telemetry

    def _owned(self, address):
        for w in self.wallets:
            try:
                if self.rpc.get_address_info(w, address).get("ismine"):
                    return True
            except RpcError:
                continue
        return False

    def _authorize_distribution_addresses(
            self,
            addresses,
            allowed_external_addresses,
    ):
        allowed_external = frozenset(allowed_external_addresses or ())

        for address in addresses:
            if self._owned(address):
                continue

            if address not in allowed_external:
                raise BuildError(
                    "distribution destination is not one of the experiment "
                    "wallets or an explicitly approved external address"
                )

            try:
                valid = self.rpc.validate_address(address)
            except RpcError as exc:
                raise BuildError(
                    f"external destination address validation failed: {exc}"
                ) from exc

            if not isinstance(valid, dict) or valid.get("isvalid") is not True:
                raise BuildError(
                    "external distribution destination is not a valid "
                    "DigiByte address"
                )

    @staticmethod
    def _distribution_amounts(mode, items, distributable_sats):
        if (
            not isinstance(distributable_sats, int)
            or isinstance(distributable_sats, bool)
            or distributable_sats <= 0
        ):
            raise BuildError(
                "distribution amount must be a positive integer number "
                "of satoshis"
            )

        if mode == "percentage":
            basis_points = [item["percent_bps"] for item in items]

            if sum(basis_points) != 10_000:
                raise BuildError(
                    "percentage distribution must total exactly 10000 "
                    "basis points"
                )

            numerators = [
                distributable_sats * bps
                for bps in basis_points
            ]

            amounts = [
                numerator // 10_000
                for numerator in numerators
            ]

            remainder = distributable_sats - sum(amounts)

            # Deterministic largest-remainder allocation. Equal fractional
            # remainders are resolved by the original destination order.
            order = sorted(
                range(len(items)),
                key=lambda i: (
                    -(numerators[i] % 10_000),
                    i,
                ),
            )

            for i in order[:remainder]:
                amounts[i] += 1

        elif mode == "fixed":
            fixed_total = sum(
                item.get("amount_sats", 0)
                for item in items
                if not item.get("remainder")
            )

            remainder_indexes = [
                i for i, item in enumerate(items)
                if item.get("remainder")
            ]

            if len(remainder_indexes) != 1:
                raise BuildError(
                    "fixed distribution requires exactly one remainder "
                    "destination"
                )

            remaining = distributable_sats - fixed_total

            if remaining <= 0:
                raise BuildError(
                    "fixed distribution leaves nothing for the remainder "
                    "destination"
                )

            amounts = []
            for item in items:
                if item.get("remainder"):
                    amounts.append(remaining)
                else:
                    amounts.append(item["amount_sats"])

        else:
            raise BuildError(
                "distribution mode must be percentage or fixed"
            )

        if any(amount <= 0 for amount in amounts):
            raise BuildError(
                "every distribution output must receive at least one satoshi"
            )

        return [
            (items[i]["address"], amounts[i])
            for i in range(len(items))
        ]

    def build_partial_distribution(
            self,
            source_wallet,
            budget_sats,
            mode,
            items,
            minconf=1,
            fee_rate=None,
            allowed_external_addresses=(),
            utxo_policy=None,
            phase=None,
            cohort_outpoints=None,
            seed_material="",
    ):
        """Build one partial multi-output distribution with retained value.

        Explicit payouts consume only part of the gross budget. The
        unassigned value remains controlled by source_wallet and absorbs
        the network fee.

        Percentage payouts are calculated from the gross budget and must
        total less than 10000 basis points.

        Fixed payouts are exact and must total less than the gross budget.
        """
        if source_wallet not in self.wallets:
            raise BuildError(
                "source wallet is not an experiment wallet"
            )

        if not isinstance(items, list) or not 1 <= len(items) <= 10:
            raise BuildError(
                "partial distribution requires between 1 and 10 destinations"
            )

        addresses = []

        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise BuildError(
                    f"partial distribution item {i + 1} must be an object"
                )

            address = item.get("address")

            if not isinstance(address, str) or not address:
                raise BuildError(
                    f"partial distribution item {i + 1} requires an address"
                )

            addresses.append(address)

            if mode == "percentage":
                if set(item) != {"address", "percent_bps"}:
                    raise BuildError(
                        "percentage partial distribution items require "
                        "exactly address and percent_bps"
                    )

                bps = item["percent_bps"]

                if (
                    not isinstance(bps, int)
                    or isinstance(bps, bool)
                    or bps <= 0
                    or bps > 10_000
                ):
                    raise BuildError(
                        "percent_bps must be an integer in 1..10000"
                    )

            elif mode == "fixed":
                if set(item) != {"address", "amount_sats"}:
                    raise BuildError(
                        "fixed partial distribution items require "
                        "exactly address and amount_sats"
                    )

                amount = item["amount_sats"]

                if (
                    not isinstance(amount, int)
                    or isinstance(amount, bool)
                    or amount <= 0
                ):
                    raise BuildError(
                        "fixed amount_sats must be a positive integer"
                    )

            else:
                raise BuildError(
                    "partial distribution mode must be percentage or fixed"
                )

        if len(set(addresses)) != len(addresses):
            raise BuildError(
                "partial distribution destination addresses must be unique"
            )

        self._authorize_distribution_addresses(
            addresses,
            allowed_external_addresses,
        )

        if budget_sats != "all":
            if (
                not isinstance(budget_sats, int)
                or isinstance(budget_sats, bool)
                or budget_sats <= 0
            ):
                raise BuildError(
                    "partial distribution budget must be a positive integer "
                    "number of satoshis or \"all\""
                )

            budget = budget_sats

        elif utxo_policy is None:
            coins = [
                u
                for u in self.rpc.list_unspent(source_wallet, minconf)
                if u.get("spendable", True) and u.get("safe", True)
            ]

            if not coins:
                raise BuildError(
                    "nothing to distribute: the wallet has no confirmed "
                    "spendable coins"
                )

            budget = sum(
                to_sats(u["amount"])
                for u in coins
            )

        else:
            chosen, _ = self._policy_select_inputs(
                source_wallet,
                None,
                sweep=True,
                utxo_policy=utxo_policy,
                phase=phase,
                cohort_outpoints=cohort_outpoints,
                seed_material=seed_material,
                reserve_fee=False,
            )

            if not chosen:
                raise BuildError(
                    "nothing to distribute: UTXO policy produced "
                    "no eligible inputs"
                )

            budget = sum(
                u["amount_sats"]
                if "amount_sats" in u
                else to_sats(u["amount"])
                for u in chosen
            )

        if mode == "percentage":
            total_bps = sum(
                item["percent_bps"]
                for item in items
            )

            if total_bps >= 10_000:
                raise BuildError(
                    "partial percentage distribution must total "
                    "less than 10000 basis points"
                )

            payout_items = []

            for item in items:
                amount = (
                    budget * item["percent_bps"]
                ) // 10_000

                if amount <= 0:
                    raise BuildError(
                        "every partial distribution output must "
                        "receive at least one satoshi"
                    )

                payout_items.append({
                    "address": item["address"],
                    "amount_sats": amount,
                })

        else:
            fixed_total = sum(
                item["amount_sats"]
                for item in items
            )

            if fixed_total >= budget:
                raise BuildError(
                    "fixed partial distribution payouts must leave "
                    "retained value for the network fee"
                )

            payout_items = [
                dict(item)
                for item in items
            ]

        payout_total = sum(
            item["amount_sats"]
            for item in payout_items
        )

        if payout_total >= budget:
            raise BuildError(
                "partial distribution payouts must leave retained value "
                "for the network fee"
            )

        retained_address = self.rpc.get_new_address(
            source_wallet,
            "flowlab-retained",
        )

        if retained_address in addresses:
            raise BuildError(
                "generated retained-value address collides with a "
                "partial distribution destination"
            )

        terminal_items = [
            *payout_items,
            {
                "address": retained_address,
                "remainder": True,
            },
        ]

        prepared = self.build_distribution(
            source_wallet,
            budget,
            "fixed",
            terminal_items,
            minconf=minconf,
            fee_rate=fee_rate,
            allowed_external_addresses=allowed_external_addresses,
            utxo_policy=utxo_policy,
            phase=phase,
            cohort_outpoints=cohort_outpoints,
            seed_material=seed_material,
        )

        payout_destinations = tuple(
            (
                item["address"],
                item["amount_sats"],
            )
            for item in payout_items
        )

        return PreparedDistribution(
            source_wallet=prepared.source_wallet,
            budget_sats=prepared.budget_sats,
            distributed_sats=sum(
                amount
                for _, amount in payout_destinations
            ),
            change_address=retained_address,
            hex=prepared.hex,
            txid=prepared.txid,
            fee_sats=prepared.fee_sats,
            inputs=prepared.inputs,
            outputs=prepared.outputs,
            destinations=payout_destinations,
            utxo_selection=prepared.utxo_selection,
        )


    def build_distribution(
            self,
            source_wallet,
            budget_sats,
            mode,
            items,
            minconf=1,
            fee_rate=None,
            allowed_external_addresses=(),
            utxo_policy=None,
            phase=None,
            cohort_outpoints=None,
            seed_material="",
    ):
        """Build one exact, multi-output terminal distribution transaction.

        budget_sats is the gross finalization budget INCLUDING the network
        fee, or "all" to consume every confirmed spendable input.

        percentage items use integer percent_bps totaling 10000.

        fixed items use positive amount_sats plus exactly one remainder item.
        The fixed outputs remain exact and the remainder absorbs the fee.

        External addresses must be explicitly listed in
        allowed_external_addresses. Merely being a valid address is not enough.
        """
        if source_wallet not in self.wallets:
            raise BuildError(
                "source wallet is not an experiment wallet"
            )

        if not isinstance(items, list) or not 1 <= len(items) <= 10:
            raise BuildError(
                "distribution requires between 1 and 10 destinations"
            )

        addresses = []

        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise BuildError(
                    f"distribution item {i + 1} must be an object"
                )

            address = item.get("address")
            if not isinstance(address, str) or not address:
                raise BuildError(
                    f"distribution item {i + 1} requires an address"
                )

            addresses.append(address)

            if mode == "percentage":
                if set(item) != {"address", "percent_bps"}:
                    raise BuildError(
                        "percentage distribution items require exactly "
                        "address and percent_bps"
                    )

                bps = item["percent_bps"]

                if (
                    not isinstance(bps, int)
                    or isinstance(bps, bool)
                    or bps <= 0
                    or bps > 10_000
                ):
                    raise BuildError(
                        "percent_bps must be an integer in 1..10000"
                    )

            elif mode == "fixed":
                keys = set(item)

                amount_form = keys == {"address", "amount_sats"}
                remainder_form = (
                    keys == {"address", "remainder"}
                    and item.get("remainder") is True
                )

                if not (amount_form or remainder_form):
                    raise BuildError(
                        "fixed distribution items require either "
                        "address + amount_sats or address + remainder=true"
                    )

                if amount_form:
                    amount = item["amount_sats"]

                    if (
                        not isinstance(amount, int)
                        or isinstance(amount, bool)
                        or amount <= 0
                    ):
                        raise BuildError(
                            "fixed amount_sats must be a positive integer"
                        )

            else:
                raise BuildError(
                    "distribution mode must be percentage or fixed"
                )

        if len(set(addresses)) != len(addresses):
            raise BuildError(
                "distribution destination addresses must be unique"
            )

        if mode == "percentage":
            if sum(item["percent_bps"] for item in items) != 10_000:
                raise BuildError(
                    "percentage distribution must total exactly 10000 "
                    "basis points"
                )

        if mode == "fixed":
            if sum(bool(item.get("remainder")) for item in items) != 1:
                raise BuildError(
                    "fixed distribution requires exactly one remainder "
                    "destination"
                )

        self._authorize_distribution_addresses(
            addresses,
            allowed_external_addresses,
        )

        if budget_sats != "all":
            if (
                not isinstance(budget_sats, int)
                or isinstance(budget_sats, bool)
                or budget_sats <= 0
            ):
                raise BuildError(
                    "distribution budget must be a positive integer number "
                    "of satoshis or \"all\""
                )

        selection_telemetry = None

        if utxo_policy is None:
            # Legacy compatibility path.
            coins = [
                u for u in self.rpc.list_unspent(source_wallet, minconf)
                if u.get("spendable", True) and u.get("safe", True)
            ]
            coins.sort(
                key=lambda u: (
                    -to_sats(u["amount"]),
                    u["txid"],
                    u["vout"],
                )
            )

            if budget_sats == "all":
                chosen = list(coins)
                total = sum(
                    to_sats(u["amount"])
                    for u in chosen
                )

                if not chosen:
                    raise BuildError(
                        "nothing to distribute: the wallet has no confirmed "
                        "spendable coins"
                    )

                budget = total

            else:
                chosen = []
                total = 0

                for u in coins:
                    chosen.append(u)
                    total += to_sats(u["amount"])

                    if total >= budget_sats:
                        break

                if total < budget_sats:
                    raise BuildError(
                        f"insufficient funds: have {total} sats, "
                        f"distribution budget is {budget_sats}"
                    )

                budget = budget_sats

        else:
            sweep = budget_sats == "all"

            chosen, selection_telemetry = (
                self._policy_select_inputs(
                    source_wallet,
                    None if sweep else budget_sats,
                    sweep=sweep,
                    utxo_policy=utxo_policy,
                    phase=phase,
                    cohort_outpoints=cohort_outpoints,
                    seed_material=seed_material,
                    reserve_fee=False,
                )
            )

            total = sum(
                u["amount_sats"]
                if "amount_sats" in u
                else to_sats(u["amount"])
                for u in chosen
            )

            if sweep:
                if not chosen:
                    raise BuildError(
                        "nothing to distribute: UTXO policy produced "
                        "no eligible inputs"
                    )

                budget = total

            else:
                if total < budget_sats:
                    raise BuildError(
                        "approved UTXO policy selected insufficient "
                        "value for the distribution budget"
                    )

                budget = budget_sats

        value_of = {
            (u["txid"], u["vout"]): to_sats(u["amount"])
            for u in chosen
        }

        change = self.rpc.get_new_address(
            source_wallet,
            "flowlab-change",
        )

        if change in addresses:
            raise BuildError(
                "generated change address collides with a distribution "
                "destination"
            )

        # ------------------------------------------------------
        # Fee probe.
        #
        # Build a gross-budget transaction and let Core subtract the
        # fee from one provisional terminal output. This establishes the
        # fee while preserving the same input/output-count shape used by
        # the final transaction.

        provisional = self._distribution_amounts(
            mode,
            items,
            budget,
        )

        if mode == "fixed":
            fee_index = next(
                i for i, item in enumerate(items)
                if item.get("remainder")
            )
        else:
            fee_index = max(
                range(len(provisional)),
                key=lambda i: provisional[i][1],
            )

        raw_probe = self.rpc.create_raw_transaction(
            [
                {"txid": u["txid"], "vout": u["vout"]}
                for u in chosen
            ],
            dict(provisional),
        )

        try:
            funded_probe = self.rpc.fund_raw_transaction(
                source_wallet,
                raw_probe,
                change,
                fee_rate,
                subtract_fee_indexes=[fee_index],
            )
        except RpcError as exc:
            raise BuildError(
                f"distribution fee probe failed: {exc}"
            ) from exc

        fee = to_sats(funded_probe.get("fee", 0))

        if fee < 0 or fee > self.max_fee_sats:
            raise BuildError(
                f"fee {fee} sats is outside the allowed range "
                f"0..{self.max_fee_sats}"
            )

        net = budget - fee

        if net <= 0:
            raise BuildError(
                "distribution budget does not cover the network fee"
            )

        destinations = self._distribution_amounts(
            mode,
            items,
            net,
        )

        # ------------------------------------------------------
        # Exact final transaction.

        raw = self.rpc.create_raw_transaction(
            [
                {"txid": u["txid"], "vout": u["vout"]}
                for u in chosen
            ],
            dict(destinations),
        )

        try:
            funded = self.rpc.fund_raw_transaction(
                source_wallet,
                raw,
                change,
                fee_rate,
            )
        except RpcError as exc:
            raise BuildError(
                f"distribution funding failed: {exc}"
            ) from exc

        final_fee = to_sats(funded.get("fee", 0))

        if final_fee != fee:
            raise BuildError(
                "distribution fee changed between probe and final build; "
                "refusing"
            )

        signed = self.rpc.sign_raw_transaction(
            source_wallet,
            funded["hex"],
        )

        if signed.get("complete") is not True:
            raise BuildError(
                "distribution transaction was not fully signed"
            )

        hexstr = signed["hex"]
        dec = self.rpc.decode_raw_transaction(hexstr)

        ins = {
            (i["txid"], i["vout"])
            for i in dec["vin"]
        }

        if ins != set(value_of):
            raise BuildError(
                "distribution transaction inputs are not the ones "
                "that were chosen"
            )

        outs = [
            (
                o["scriptPubKey"].get("address"),
                to_sats(o["value"]),
            )
            for o in dec["vout"]
        ]

        expected = list(destinations)
        expected_change = total - budget

        if expected_change:
            expected.append((change, expected_change))

        if len(outs) != len(expected):
            raise BuildError(
                "distribution transaction has an unexpected number "
                "of outputs"
            )

        actual_by_address = {}

        for address, sats in outs:
            if address in actual_by_address:
                raise BuildError(
                    "distribution transaction contains duplicate "
                    "output addresses"
                )
            actual_by_address[address] = sats

        expected_by_address = dict(expected)

        if actual_by_address != expected_by_address:
            raise BuildError(
                "distribution transaction outputs do not exactly match "
                "the approved distribution"
            )

        calculated_fee = (
            sum(value_of.values())
            - sum(sats for _, sats in outs)
        )

        if calculated_fee != fee:
            raise BuildError(
                f"distribution fee was {calculated_fee} sats, "
                f"expected {fee}"
            )

        verdict = self.rpc.test_mempool_accept(hexstr)[0]

        if verdict.get("allowed") is not True:
            raise BuildError(
                "node would reject distribution transaction: "
                f"{verdict.get('reject-reason')}"
            )

        return PreparedDistribution(
            source_wallet=source_wallet,
            budget_sats=budget,
            distributed_sats=sum(
                sats for _, sats in destinations
            ),
            change_address=change,
            hex=hexstr,
            txid=dec["txid"],
            fee_sats=fee,
            inputs=tuple(sorted(ins)),
            outputs=tuple(outs),
            destinations=tuple(destinations),
            utxo_selection=selection_telemetry,
        )

    def build(
            self,
            source_wallet,
            address,
            amount_sats,
            minconf=1,
            fee_rate=None,
            allow_external=False,
            utxo_policy=None,
            phase=None,
            cohort_outpoints=None,
            seed_material="",
    ):
        if source_wallet not in self.wallets:
            raise BuildError("source wallet is not an experiment wallet")
        sweep = amount_sats == "all"
        if not sweep and (not isinstance(amount_sats, int) or isinstance(amount_sats, bool)
                          or amount_sats <= 0):
            raise BuildError("amount must be a positive integer number of satoshis")

        owned = self._owned(address)

        if not owned:
            if not allow_external:
                raise BuildError(
                    "destination is not one of the experiment wallets; refusing"
                )

            try:
                valid = self.rpc.validate_address(address)
            except RpcError as exc:
                raise BuildError(
                    f"external destination address validation failed: {exc}"
                ) from exc

            if not isinstance(valid, dict) or valid.get("isvalid") is not True:
                raise BuildError("external destination is not a valid DigiByte address")

        selection_telemetry = None

        if utxo_policy is None:
            # Legacy compatibility path. Keep the original FlowLab
            # largest-first behavior unchanged.
            coins = [
                u for u in self.rpc.list_unspent(source_wallet, minconf)
                if u.get("spendable", True) and u.get("safe", True)
            ]
            coins.sort(
                key=lambda u: (
                    -to_sats(u["amount"]),
                    u["txid"],
                    u["vout"],
                )
            )

            chosen, total = [], 0

            if sweep:
                chosen = coins
                total = sum(
                    to_sats(u["amount"])
                    for u in coins
                )

                if not chosen:
                    raise BuildError(
                        "nothing to send: the wallet has no confirmed "
                        "spendable coins"
                    )

                request = total
                reserve = self._fee_reserve(len(chosen))

                if total <= reserve:
                    raise BuildError(
                        f"balance {total} sats does not cover "
                        f"the fee reserve {reserve}"
                    )

            else:
                request = amount_sats

                for u in coins:
                    chosen.append(u)
                    total += to_sats(u["amount"])

                    if (
                        total
                        >= amount_sats
                        + self._fee_reserve(len(chosen))
                    ):
                        break

                reserve = self._fee_reserve(
                    max(1, len(chosen))
                )

                if total < amount_sats + reserve:
                    raise BuildError(
                        f"insufficient funds: have {total} sats, need "
                        f"{amount_sats} plus a fee reserve of {reserve}"
                    )

        else:
            chosen, selection_telemetry = (
                self._policy_select_inputs(
                    source_wallet,
                    amount_sats,
                    sweep=sweep,
                    utxo_policy=utxo_policy,
                    phase=phase,
                    cohort_outpoints=cohort_outpoints,
                    seed_material=seed_material,
                )
            )

            total = sum(
                u["amount_sats"]
                if "amount_sats" in u
                else to_sats(u["amount"])
                for u in chosen
            )

            reserve = self._fee_reserve(
                max(1, len(chosen))
            )

            if sweep:
                request = total

                if total <= reserve:
                    raise BuildError(
                        f"balance {total} sats does not cover "
                        f"the fee reserve {reserve}"
                    )

            else:
                request = amount_sats

                if total < amount_sats + reserve:
                    raise BuildError(
                        "approved UTXO policy selected insufficient "
                        "value after fee reserve"
                    )
        value_of = {(u["txid"], u["vout"]): to_sats(u["amount"]) for u in chosen}

        change = self.rpc.get_new_address(source_wallet, "flowlab-change")
        raw = self.rpc.create_raw_transaction(
            [{"txid": u["txid"], "vout": u["vout"]} for u in chosen], {address: request})
        funded = self.rpc.fund_raw_transaction(source_wallet, raw, change, fee_rate,
                                               subtract_fee=sweep)
        signed = self.rpc.sign_raw_transaction(source_wallet, funded["hex"])
        if signed.get("complete") is not True:
            raise BuildError("transaction was not fully signed")
        hexstr = signed["hex"]

        dec = self.rpc.decode_raw_transaction(hexstr)
        ins = {(i["txid"], i["vout"]) for i in dec["vin"]}
        if ins != set(value_of):
            raise BuildError("transaction inputs are not the ones that were chosen")
        outs = []
        for o in dec["vout"]:
            outs.append((o["scriptPubKey"].get("address"), to_sats(o["value"])))
        dest = [o for o in outs if o[0] == address]
        others = [o for o in outs if o[0] != address]
        if sweep:
            if len(dest) != 1 or others:
                raise BuildError("a full-balance send must have exactly one output: the destination")
            amount_sats = dest[0][1]
        elif len(dest) != 1 or dest[0][1] != amount_sats:
            raise BuildError("destination output is not exactly the requested amount")
        if len(others) > 1 or any(a != change for a, _ in others):
            raise BuildError("unexpected output (only destination and our own change allowed)")
        fee = sum(value_of.values()) - sum(v for _, v in outs)
        if fee < 0 or fee > self.max_fee_sats:
            raise BuildError(f"fee {fee} sats is outside the allowed range 0..{self.max_fee_sats}")
        verdict = self.rpc.test_mempool_accept(hexstr)[0]
        if verdict.get("allowed") is not True:
            raise BuildError(f"node would reject it: {verdict.get('reject-reason')}")
        return Prepared(
            source_wallet,
            address,
            amount_sats,
            change,
            hexstr,
            dec["txid"],
            fee,
            tuple(sorted(ins)),
            tuple(outs),
            selection_telemetry,
        )


def broadcast_distribution(
        engine,
        rpc,
        job_id,
        prepared,
        resolved_destinations,
):
    """Journal and broadcast one prepared terminal distribution.

    resolved_destinations is the executor-approved logical destination
    list enriched with each exact resolved address and final satoshi
    amount. It is persisted before sendrawtransaction so recovery and
    reconciliation never need to guess where an internal wallet item
    resolved.
    """
    if not isinstance(resolved_destinations, list):
        raise BuildError(
            "resolved distribution destinations must be a list"
        )

    if len(resolved_destinations) != len(prepared.destinations):
        raise BuildError(
            "resolved destination count does not match prepared "
            "distribution"
        )

    prepared_by_address = dict(prepared.destinations)

    normalized = []

    for item in resolved_destinations:
        if not isinstance(item, dict):
            raise BuildError(
                "resolved distribution destination must be an object"
            )

        address = item.get("resolved_address")

        if address not in prepared_by_address:
            raise BuildError(
                "resolved distribution address is not present in "
                "the prepared transaction"
            )

        row = dict(item)
        row["amount_sats"] = prepared_by_address[address]
        normalized.append(row)

    if {
        item["resolved_address"]
        for item in normalized
    } != set(prepared_by_address):
        raise BuildError(
            "resolved distribution addresses do not exactly match "
            "the prepared transaction"
        )

    payload = {
        "source_wallet": prepared.source_wallet,
        "budget_sats": prepared.budget_sats,
        "distributed_sats": prepared.distributed_sats,
        "expected_txid": prepared.txid,
        "fee_sats": prepared.fee_sats,
        "destinations": normalized,
        "tx_sha256": hashlib.sha256(
            prepared.hex.encode()
        ).hexdigest(),
    }

    if prepared.utxo_selection is not None:
        payload["utxo_selection"] = prepared.utxo_selection

    action_id = engine.begin_action(
        job_id,
        "broadcast",
        payload,
    )

    try:
        txid = rpc.send_raw_transaction(prepared.hex)
    except RpcError as e:
        definite = (
            e.code == -26
            and "already" not in str(e).lower()
        )

        if definite:
            engine.fail_action(action_id, str(e))

        # As with the legacy broadcast path, ambiguous send failures
        # remain unresolved for explicit recovery.
        raise

    result = {
        "txid": txid,
        "fee_sats": prepared.fee_sats,
        "budget_sats": prepared.budget_sats,
        "distributed_sats": prepared.distributed_sats,
        "destinations": normalized,
    }

    if prepared.utxo_selection is not None:
        result["utxo_selection"] = prepared.utxo_selection

    engine.complete_action(
        action_id,
        result,
    )

    if txid != prepared.txid:
        raise BuildError(
            f"node returned txid {txid}, expected "
            f"{prepared.txid} (recorded as sent)"
        )

    return txid


def broadcast(engine, rpc, job_id, prepared):
    """The ONLY path to sendrawtransaction. Journal first, send second, record third."""
    payload = {"amount_sats": prepared.amount_sats, "address": prepared.address,
               "source_wallet": prepared.source_wallet, "expected_txid": prepared.txid,
               "fee_sats": prepared.fee_sats,
               "tx_sha256": hashlib.sha256(prepared.hex.encode()).hexdigest()}

    if prepared.utxo_selection is not None:
        payload["utxo_selection"] = prepared.utxo_selection

    action_id = engine.begin_action(job_id, "broadcast", payload)
    try:
        txid = rpc.send_raw_transaction(prepared.hex)
    except RpcError as e:
        definite = e.code == -26 and "already" not in str(e).lower()
        if definite:
            engine.fail_action(action_id, str(e))
        # Anything else (timeout, dropped connection, "already known") may mean the
        # node HAS the transaction: leave the intent in place for recovery to settle.
        raise
    result = {
        "txid": txid,
        "fee_sats": prepared.fee_sats,
        "amount_sats": prepared.amount_sats,
        "address": prepared.address,
    }

    if prepared.utxo_selection is not None:
        result["utxo_selection"] = prepared.utxo_selection

    engine.complete_action(
        action_id,
        result,
    )
    if txid != prepared.txid:
        raise BuildError(f"node returned txid {txid}, expected {prepared.txid} (recorded as sent)")
    return txid
