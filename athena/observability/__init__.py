"""Callback-based monitoring for Athena (course rule 5).

The pipeline talks to ``Recorder``; the event log, the ADK plugin, the metrics
and the heatmap live behind it. Monitoring is off when ATHENA_MONITORING=0.
"""

from athena.observability.context import agent_context, current_agent
from athena.observability.events import Event, EventKind
from athena.observability.recorder import (
    Recorder,
    get_recorder,
    monitoring_enabled,
    set_recorder,
)
from athena.observability.store import EventStore, SQLiteEventStore

__all__ = [
    "Event",
    "EventKind",
    "EventStore",
    "Recorder",
    "SQLiteEventStore",
    "agent_context",
    "current_agent",
    "get_recorder",
    "monitoring_enabled",
    "set_recorder",
]
