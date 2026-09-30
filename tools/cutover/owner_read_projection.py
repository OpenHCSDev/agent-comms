"""Explicit producer declaration for the existing acquired owner read.

The release owner selects the authentic producer runtime and its format. Neither
member discovers formats from missing fields or offers a fallback reader.
"""
from abc import abstractmethod

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.registry_document import RegistryDocument
from thread_format_retirement import GoalReportMemberRetirement


class OwnerReadProjection(DeclaredFamily, affix='Projection'):
    @classmethod
    @abstractmethod
    def project(cls, document: RegistryDocument) -> dict:
        """Project a document already validated by its authentic producer."""


class RetiredGoalReportProjection(OwnerReadProjection):
    @classmethod
    def project(cls, document: RegistryDocument) -> dict:
        return GoalReportMemberRetirement.threads(FieldCodec.encode(document))


class CurrentThreadProjection(OwnerReadProjection):
    @classmethod
    def project(cls, document: RegistryDocument) -> dict:
        return FieldCodec.encode(document)
