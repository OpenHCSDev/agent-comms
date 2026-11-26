"""The actual stored session fields and their configuration roles."""
from abc import ABC
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, ClassVar

from python_introspect import FieldLabel

from agent_comms.execution.pi_vocabulary import NoThinkingOverride, ServiceTier, ThinkingLevelField
from agent_comms.nominal.configuration_record import ConfigurationRecord
from agent_comms.nominal.field_codec import Catalog, Description, RegistrationPolicy
from agent_comms.nominal.registration_inheritance import InheritApplied, InheritPrevious
from agent_comms.registry.thread_model import ThreadModel, UndeclaredModel


class ConfigurationSlot(Catalog):
    """The role whose catalog and writer compose this stored field."""


class SettingRequest(ABC):
    """Runtime setting command implemented by Pi RPC SettingCommand."""


class ModelSetting:
    category: ClassVar[str] = "model"


class ThinkingSetting:
    category: ClassVar[str] = "thought_level"


class ServiceTierSetting:
    category: ClassVar[str] = "model_config"


class ContextEditSetting:
    category: ClassVar[str] = "context_edit_policy"


class ContextEditPolicy(StrEnum):
    """Whether an agent run may finish without updating its live context."""

    OPTIONAL = "optional"
    REQUIRED_EACH_TURN = "required_each_turn"

    def environment(self) -> dict[str, str]:
        return {"CLM_CONTEXT_EDIT_POLICY": self.value}


@dataclass(frozen=True)
class ThreadSettings(ConfigurationRecord):
    model: Annotated[
        ThreadModel, ConfigurationSlot(ModelSetting), FieldLabel("Model"),
        Description("Model for this thread; a running turn switches at its next model call"),
        RegistrationPolicy(InheritApplied),
    ] = field(default_factory=UndeclaredModel)
    thinking_level: Annotated[
        ThinkingLevelField, ConfigurationSlot(ThinkingSetting), FieldLabel("Thinking level"),
        Description("Thinking level for this thread; a running turn switches at its next model call"),
        RegistrationPolicy(InheritApplied),
    ] = NoThinkingOverride
    service_tier: Annotated[
        ServiceTier, ConfigurationSlot(ServiceTierSetting), FieldLabel("Service tier"),
        Description("Provider request service tier, applied from this thread's next turn"),
        RegistrationPolicy(InheritPrevious),
    ] = field(default_factory=ServiceTier.default, kw_only=True)
    context_edit_policy: Annotated[
        ContextEditPolicy,
        ConfigurationSlot(ContextEditSetting),
        FieldLabel("Context edits"),
        Description("Whether every agent turn must apply an accepted live-context edit"),
        RegistrationPolicy(InheritPrevious),
    ] = field(default=ContextEditPolicy.OPTIONAL, kw_only=True)
