# EXP-DDF57EE4 — First Staged Mainnet Validation

Date: 2026-10-06

## Purpose

Validate FlowLab's staged experimental allocation architecture on DigiByte
mainnet using a deliberately small Random Walk experiment.

The run tests the invariant that the full approved experiment principal is
first committed from the reserve wallet into an empty allocation/stage wallet,
after which workload and finalization fees are paid from experiment principal.

The reserve-to-stage commitment fee remains outside experiment principal.

## Approved Configuration

Experiment:

    EXP-DDF57EE4

Config hash:

    cd153a430370c2928c3dddc0b966d48772040bca63ad80b732e6f6fc50c19af9

Randomization seed:

    3718171995

Roles:

    Reserve:      flab_source
    Stage:        flab_stage
    Workers:      flab_a, flab_b
    Destination:  flab_dest

Approved principal:

    25,000,000 sats
    0.25000000 DGB

Workload:

    Decisions:       2
    Amount minimum:  5,000,000 sats
    Amount maximum:  8,000,000 sats
    Delay minimum:   5 seconds
    Delay maximum:   10 seconds
    Confirmations:   2

Approved topology:

    flab_stage -> flab_a
    flab_stage -> flab_b
    flab_a     -> flab_b
    flab_b     -> flab_a

Finalization:

    flab_stage -> flab_dest
    flab_a     -> flab_dest
    flab_b     -> flab_dest

## Preflight

Before execution:

    flab_stage:
      trusted = 0
      untrusted_pending = 0
      immature = 0

    flab_a:
      trusted = 0
      untrusted_pending = 0
      immature = 0

    flab_b:
      trusted = 0
      untrusted_pending = 0
      immature = 0

The stage-contamination guard was active during this run.

## Actual Execution

### Allocation commitment

    flab_source -> flab_stage
    amount: 0.25000000 DGB
    fee:    0.01410000 DGB
    txid:   b11e4cefcd73909bfaebab11a8ef814f98b30ee570def05ae42c59b7ae06e873

The stage received the full approved principal.

The commitment fee was paid separately by the reserve and is not part of
experiment principal.

### Workload decision 1

    flab_stage -> flab_a
    amount: 0.07392184 DGB
    fee:    0.01410000 DGB
    txid:   345d82b00408d06b777a11d4b35100856c73fc3c181b460cb1fb642b7a9924cd

### Workload decision 2

    flab_stage -> flab_b
    amount: 0.07030078 DGB
    fee:    0.01410000 DGB
    txid:   fe09951f68a7fc64aec2d3391df1f6de7c304400d3b79619cc4cf948678d2383

### Finalization sweep 1

    flab_stage -> flab_dest
    amount: 0.06657738 DGB
    fee:    0.01100000 DGB
    txid:   4e1787ecbcb534b1e2583b1ff788fb2d5ea842f172bbad47b6e463a96c2c226e

### Finalization sweep 2

    flab_a -> flab_dest
    amount: 0.06292184 DGB
    fee:    0.01100000 DGB
    txid:   020b36596b7fc3cc1d7aa785993a1767bee68f5096a7fb8c5152c58898a5ca9b

### Finalization sweep 3

    flab_b -> flab_dest
    amount: 0.05930078 DGB
    fee:    0.01100000 DGB
    txid:   45d58d4e0a5952af097a8751aac9c995e79bc514a480d4177212b0a41c5f9530

## Accounting

Approved principal:

    0.25000000 DGB

Post-commit experiment fees:

    workload fee 1:      0.01410000
    workload fee 2:      0.01410000
    finalization fee 1:  0.01100000
    finalization fee 2:  0.01100000
    finalization fee 3:  0.01100000
                         ----------
                         0.06120000 DGB

Destination receipts:

    0.06657738
    0.06292184
    0.05930078
    ----------
    0.18880000 DGB

Principal invariant:

    0.25000000 principal
  - 0.06120000 post-commit network fees
  = 0.18880000 destination receipts

PASS.

Reserve-side commitment fee:

    0.01410000 DGB

Total network fees reported by FlowLab:

    0.07530000 DGB

This equals:

    0.01410000 reserve-side commitment fee
  + 0.06120000 experiment fees
  = 0.07530000 total fees

PASS.

## Completion

FlowLab reported:

    completed and reconciled
    run ended (code 0)

At completion:

    flab_stage = 0
    flab_a     = 0
    flab_b     = 0

All committed experiment principal either reached the destination or was
consumed by approved post-commit network fees.

## Findings

### Confirmed

1. Full principal is committed to the stage before workload begins.
2. Reserve pays the commitment transaction fee outside experiment principal.
3. Workload cannot begin before allocation commitment confirms.
4. Seeded Random Walk generated only transitions from the approved topology.
5. Finalization emptied stage and worker wallets into the destination.
6. Principal accounting reconciled exactly.
7. Stage contamination protection was active for the live run.
8. Historical reserve funds were not swept during finalization.

### Dashboard findings

The live run exposed presentation issues rather than execution issues.

Wallet ordering currently follows the flat RPC allowlist instead of logical
roles. Dashboard wallet presentation should use:

    Reserve
    Stage
    Workers
    Hubs
    Destination

The old experimental telemetry fields:

    SOURCE BUDGET USED
    SOURCE BUDGET REMAINING

no longer correctly describe staged experiments.

Future staged telemetry should distinguish at least:

    committed principal
    principal remaining
    cumulative workload
    experiment fees
    reserve-side commitment fee
    finalization progress

## Result

The staged allocation architecture passed its first DigiByte mainnet
validation.
