"""OpenHCS agent communications — import each API from its defining module."""

import copyreg
from collections.abc import Mapping
from types import MappingProxyType

__version__ = "0.1.0"


def read_only_view[K, V](mapping: Mapping[K, V]) -> MappingProxyType[K, V]:
    """Rebuild a read-only view on the receiving side of a pickle."""
    return MappingProxyType(dict(mapping))


# Frozen records hold read-only views, and observation answers cross the local pipe as
# pickles; a view crosses as the same view of a copy of its mapping.
copyreg.pickle(MappingProxyType, lambda view: (read_only_view, (dict(view),)))
