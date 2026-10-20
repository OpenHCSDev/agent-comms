"""External annotation permission is an original human wire declaration."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .message_reference import MessageReference


@dataclass(frozen=True)
class AnnotationPolicy(DeclaredFamily, affix="AnnotationPolicy"):
    @abstractmethod
    def observe(self, worker, session_id, manifest, values): ...


@dataclass(frozen=True)
class DisabledAnnotationPolicy(AnnotationPolicy):
    def observe(self, worker, session_id, manifest, values):
        pass


@dataclass(frozen=True)
class GrantedAnnotationPolicy(AnnotationPolicy):
    source: MessageReference

    def observe(self, worker, session_id, manifest, values):
        worker.start(self, session_id, manifest, values)
