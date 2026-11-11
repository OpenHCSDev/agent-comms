"""An original runtime observation projects only the fields it actually observed."""

from abc import ABC, abstractmethod


class RuntimeInfoProjection[Info](ABC):
    __slots__ = ()

    @abstractmethod
    def project_runtime_info(self, current: Info) -> Info: ...
