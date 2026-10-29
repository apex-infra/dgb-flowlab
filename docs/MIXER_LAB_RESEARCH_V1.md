# DigiByte FlowLab — Mixer Lab Research V1

Release: Research V1
Branch: experimental-engine-v1
Validated implementation: 4608f0d
Execution: Synthetic research only

## Overview
Mixer Lab Research V1 provides deterministic synthetic UTXO experiments.
It evaluates ownership-inference heuristics using controlled ground truth.
It does not execute real mixing or blockchain transactions.

## Research Components
- Synthetic UTXO workload generation
- Multi-seed research campaigns
- Transaction ancestry analysis
- Statistical reporting and paired comparisons
- Public and restricted research exports
- Integrity attestations
- Public UTXO graph generation
- Validated research API

## Dashboard
- Synthetic experiment configuration
- Classification and precision analytics
- Scenario comparisons
- Collapsible detailed statistics
- Interactive public UTXO network graph
- Deterministic transaction replay
- Play, pause, restart, and speed controls
- Full-width research workspace

## Validation
- 105 targeted tests passed
- 1486 full regression tests passed
- Browser execution verified
- Graph interaction and replay verified
- M5.25 release audit passed

## Limitations
- Synthetic research does not prove blockchain anonymity.
- Results do not establish real-world address ownership.
- Replay is not live blockchain monitoring.
- Predictable seeds may allow synthetic dataset reconstruction.
- Production mixing and custody operations are outside this release.

## Release Boundary
Research V1 remains on experimental-engine-v1.
No merge into main is included.
No production wallets or DigiByte Core configurations are modified.

## Verification
Run: python3 -m unittest discover -s tests -q
Inspect: git log -1 --oneline
Inspect: git status --short
