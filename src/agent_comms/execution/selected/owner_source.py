"""The mutable original registry owner held by a selected participant."""
from abc import ABC


class RegistryOwnerSource[Owner](ABC):
    __slots__ = ()
    owner: Owner
