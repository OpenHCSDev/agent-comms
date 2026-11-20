"""Model-route metadata shared by thread creation and reconfiguration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from python_introspect import FieldLabel

from agent_comms.execution.launch_runtime import RestartEnvironment
from agent_comms.execution.native_arguments import SelectedModel
from agent_comms.execution.native_session_files import NativeModelConfiguration
from agent_comms.nominal.field_codec import Argument, Description
from agent_comms.registry.thread_model import ThreadModel


@dataclass(frozen=True, kw_only=True)
class ModelContextArguments:
    """Optional metadata for the exact provider/model route selected by a form."""

    max_context: Annotated[
        int | None,
        FieldLabel("Max context"),
        Description("Maximum context tokens advertised for the selected model route"),
        Argument(flags=("--max-context",)),
    ] = None

    def apply_model_context(
        self, configuration: RestartEnvironment, model: ThreadModel,
    ) -> None:
        if self.max_context is None:
            return
        selected = SelectedModel.decode(model.require_reported().text)
        NativeModelConfiguration.set_context_window(
            configuration.native_config, selected, self.max_context,
        )
