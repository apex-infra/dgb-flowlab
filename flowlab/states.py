"""
states.py -- the two persistent state machines, as data.

Experiment lifecycle (spec 6A) and per-flow state machine (spec Phase 1).
Transitions are table-driven: anything not listed here is illegal, and
the engine refuses it. Nothing else in the package may write a `state`
column directly.
"""

from enum import Enum


class ExperimentState(str, Enum):
    CREATED = "CREATED"
    CONFIGURED = "CONFIGURED"
    APPROVED = "APPROVED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPLETING = "COMPLETING"
    COMPLETE = "COMPLETE"
    IDLE = "IDLE"
    PAUSED = "PAUSED"
    ERROR = "ERROR"
    RECOVERY = "RECOVERY"
    ABORTED = "ABORTED"


E = ExperimentState

EXPERIMENT_TRANSITIONS = {
    E.CREATED: {E.CONFIGURED, E.ABORTED},
    E.CONFIGURED: {E.CONFIGURED, E.APPROVED, E.ABORTED},
    E.APPROVED: {E.RUNNING, E.PAUSED, E.ABORTED},
    E.RUNNING: {E.WAITING, E.COMPLETING, E.PAUSED, E.ERROR, E.RECOVERY, E.ABORTED},
    E.WAITING: {E.RUNNING, E.COMPLETING, E.PAUSED, E.ERROR, E.RECOVERY, E.ABORTED},
    E.COMPLETING: {E.COMPLETE, E.PAUSED, E.ERROR, E.RECOVERY, E.ABORTED},
    E.COMPLETE: {E.IDLE},
    E.IDLE: set(),
    E.PAUSED: {E.RUNNING, E.WAITING, E.COMPLETING, E.APPROVED, E.RECOVERY, E.ABORTED},
    E.ERROR: {E.RECOVERY, E.ABORTED},
    E.RECOVERY: {E.RUNNING, E.PAUSED, E.ERROR, E.ABORTED},
    E.ABORTED: set(),
}

# States in which the engine may be doing (or about to do) real work.
ACTIVE_STATES = {E.RUNNING, E.WAITING, E.COMPLETING}
TERMINAL_STATES = {E.IDLE, E.ABORTED}


class FlowState(str, Enum):
    START = "START"
    PLAN = "PLAN"
    EXECUTE = "EXECUTE"
    CONFIRMATION = "CONFIRMATION"
    NEXT_STATE = "NEXT_STATE"
    COMPLETE = "COMPLETE"


F = FlowState

# START -> PLAN -> EXECUTE -> CONFIRMATION -> NEXT_STATE -> (PLAN | COMPLETE)
FLOW_TRANSITIONS = {
    F.START: {F.PLAN},
    F.PLAN: {F.EXECUTE},
    F.EXECUTE: {F.CONFIRMATION},
    F.CONFIRMATION: {F.NEXT_STATE},
    F.NEXT_STATE: {F.PLAN, F.COMPLETE},
    F.COMPLETE: set(),
}

FLOW_ERROR_STATES = ("NONE", "ERROR", "RECOVERY")

# Pre-action verification (spec Phase 1): every one of these must be
# reported OK by the verifier before the engine acts. A verifier that
# omits a check FAILS CLOSED -- silence is not a pass.
REQUIRED_CHECKS = (
    "wallet",
    "balance",
    "utxo",
    "transaction",
    "confirmation",
    "height",
    "flow_state",
)
