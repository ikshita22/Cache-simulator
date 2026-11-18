"""Set-associative, snooping MESI cache simulation."""

from .model import AccessResult, CacheConfig, Simulator, State
from .trace import TraceAccess, TraceError, parse_trace

__all__ = [
    "AccessResult",
    "CacheConfig",
    "Simulator",
    "State",
    "TraceAccess",
    "TraceError",
    "parse_trace",
]
