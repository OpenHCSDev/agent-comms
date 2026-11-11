"""Resolve native launch contracts after the real request producer loads."""

import importlib
import sys
from typing import get_type_hints

importlib.import_module(sys.argv[1])

from agent_comms.transport.project_request import ProjectRuntimeRequest
from agent_comms.transport.runtime_request import RuntimeRequest
from agent_comms.transport.serving.served_requests import ServedProjectRuntimeRequest

request = RuntimeRequest.member_named(ProjectRuntimeRequest.declared_name)
assert request is ServedProjectRuntimeRequest

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.threads import Thread

assert get_type_hints(request.for_native)["snapshot"] is RegistrySnapshot
assert get_type_hints(request.for_native)["thread"] is Thread
assert get_type_hints(request.native_environment)["snapshot"] is RegistrySnapshot
assert FieldCodec.value_schema(request)
assert FieldCodec.value_schema(get_type_hints(ServedProjectRuntimeRequest.result)["return"])
print(f"Project command and launch contracts resolved after {sys.argv[1]}")
