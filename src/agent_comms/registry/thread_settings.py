"""The actual stored session fields and their configuration roles."""
from dataclasses import dataclass, field
from typing import Annotated, ClassVar

from python_introspect import FieldLabel

from agent_comms.execution.pi_vocabulary import NoThinkingOverride, ServiceTier, ThinkingLevelField
from agent_comms.nominal.configuration_record import ConfigurationRecord
from agent_comms.nominal.field_codec import Catalog, Description, RegistrationPolicy
from agent_comms.nominal.registration_inheritance import InheritApplied, InheritPrevious
from agent_comms.registry.thread_model import ThreadModel, UndeclaredModel


class ConfigurationSlot(Catalog):
    """The role whose catalog and writer compose this stored field."""


class ModelSetting:
    category: ClassVar[str] = "model"


class ThinkingSetting:
    category: ClassVar[str] = "thought_level"


class ServiceTierSetting:
    category: ClassVar[str] = "model_config"


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
