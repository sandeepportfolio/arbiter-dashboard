"""ARBITER Safety Layer — kill switch, rate-limit supervision, one-leg detection,
graceful shutdown, and market-mapping resolution criteria.

The SafetySupervisor is the single authorized path that gates ExecutionEngine
at every opportunity and fans out kill-switch/shutdown events to the dashboard.

See phase 03 (03-RESEARCH.md, 03-PATTERNS.md) for architecture and invariants.
"""
from __future__ import annotations

from ..config.settings import SafetyConfig
from .alerts import SafetyAlertTemplates
from .persistence import RedisStateShim, SafetyEventStore
from .supervisor import SafetyState, SafetySupervisor

__all__ = [
    "RedisStateShim",
    "SafetyAlertTemplates",
    "SafetyConfig",
    "SafetyEventStore",
    "SafetyState",
    "SafetySupervisor",
]
