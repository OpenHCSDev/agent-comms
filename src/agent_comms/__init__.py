"""OpenHCS agent communications — import each API from its defining module."""

__version__ = "0.1.0"

# The composition root resolves the process/store dependency without local
# imports or a second process registry. Every launch uses this store owner.
from .child_process import ChildLaunch
from .runtime_info import RuntimeInfoStore

ChildLaunch.runtime_info = RuntimeInfoStore
