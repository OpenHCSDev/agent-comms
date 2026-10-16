"""External annotation permission is an original human wire declaration."""
from __future__ import annotations

import json
import os
from abc import abstractmethod
from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .message_reference import MessageReference
from .retained_task_facts import RetainedTaskFacts
from .task_sources import AnnotationDisclosureGrant


@dataclass(frozen=True)
class AnnotationPolicy(DeclaredFamily, affix="AnnotationPolicy"):
    @classmethod
    def configured(cls):
        raw = os.environ.get("AGENT_COMMS_ANNOTATION_POLICY")
        return FieldCodec.decode(cls, json.loads(raw)) if raw else DisabledAnnotationPolicy()

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

    def acquire(self, comms, incarnation):
        snapshot = comms.registry.snapshot()
        owner = snapshot.require(incarnation.resolved(snapshot).name)
        with comms.bus.log.certified_read() as original:
            facts = RetainedTaskFacts(tuple(original.retained_task_facts(owner.incarnation)))
            current = facts.current_authored_sources(owner, snapshot)
            selected = next((message for message in current if message.reference == self.source), None)
            if selected is None:
                raise RelationViolationError("Annotation grant is absent, superseded or dropped")
            grant = selected.task.require_annotation_grant()
            grant.require_publication(selected.sender, snapshot, original)
            rules = tuple(rule for message in current
                          for rule in message.task.annotation_rules(message, facts))
        return grant, rules
