"""Independent publications retain field provenance; display never grants authority."""
import pytest

from agent_comms.agent_events import (
    ContextSizeObservation, ContextUsageObservation, ModelObservation, RuntimeInfoObservation,
)
from agent_comms.catalog_document import CatalogDocument
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord
from agent_comms.pi_counts import PiCount
from agent_comms.reading_attribution import CurrentReadingAttribution, LastKnownReadingAttribution, ReadingAttribution
from agent_comms.runtime_field import RuntimePublication, UnattributedRuntimeOrigin
from agent_comms.runtime_info import AgentRuntimeInfo, RuntimeInfoDocument, RuntimeReadScope, UnobservedRuntimeInfo
from agent_comms.threads import ChosenModel, NextTurnConfiguration
from agent_comms.thread_identity import RecordedThreadIncarnation, UnrecordedThreadIncarnation
from agent_comms.thread_model import DeclaredModel, ThreadModel, UndeclaredModel


@pytest.mark.parametrize("document_type", [RuntimeInfoDocument, CatalogDocument])
def test_current_store_format_requires_its_marker(document_type: type) -> None:
    encoded = FieldCodec.encode(document_type())
    assert isinstance(encoded, dict)
    encoded.pop("version")
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(document_type, encoded)


@pytest.fixture
def historical_runtime() -> tuple[UnrecordedThreadIncarnation, AgentRuntimeInfo]:
    owner = UnrecordedThreadIncarnation("original-spelling")
    source = UnattributedRuntimeOrigin(owner.name, owner.name, 12.0)
    observed = AgentRuntimeInfo(
        model_observation=source.capture(DeclaredModel("old/model")),
        context_used_observation=source.capture(PiCount.from_wire(25)),
        context_size_observation=source.capture(PiCount.from_wire(100)),
    )
    return owner, observed


def test_runtime_document_roundtrip_preserves_each_field_clock_and_original_scope(historical_runtime: tuple[UnrecordedThreadIncarnation, AgentRuntimeInfo]) -> None:
    owner, historical = historical_runtime
    document = RuntimeInfoDocument({owner: historical})
    restored = FieldCodec.decode(RuntimeInfoDocument, FieldCodec.encode(document))
    assert restored == document
    saved = restored.observations[owner]
    assert saved.model_observation.recorded_at() == 12.0
    assert saved.context_used_observation.recorded_at() == 12.0
    assert saved.context_size_observation.recorded_at() == 12.0
    current = RecordedThreadIncarnation("current-spelling", 20.0)
    reading = restored.read_scope(RuntimeReadScope(current, (owner.name,)))
    assert reading.model == DeclaredModel("old/model")
    assert reading.model_attribution() == LastKnownReadingAttribution((owner.name,))
    assert reading.current_model(current) == UndeclaredModel()
    assert "last known; owner unrecorded" in reading.context_reading().status_text()


@pytest.mark.parametrize("observation,supplied", [
    (ModelObservation(model=DeclaredModel("fresh/model")), "model_observation"),
    (ContextUsageObservation(context_used=PiCount.from_wire(75)), "context_used_observation"),
    (ContextSizeObservation(context_size=PiCount.from_wire(200)), "context_size_observation"),
])
def test_partial_fresh_publication_attests_only_its_supplied_field(historical_runtime: tuple[UnrecordedThreadIncarnation, AgentRuntimeInfo], observation: RuntimeInfoObservation, supplied: str) -> None:
    historical_owner, historical = historical_runtime
    owner = RecordedThreadIncarnation("current-spelling", 20.0)
    fresh = observation.project_runtime_info(AgentRuntimeInfo(), RuntimePublication(owner, 30.0))
    document = RuntimeInfoDocument({historical_owner: historical, owner: fresh})
    reading = document.read_scope(RuntimeReadScope(owner, (historical_owner.name,)))
    assert isinstance(reading, AgentRuntimeInfo)
    for plan in FieldCodec.field_plans(AgentRuntimeInfo):
        field = plan.current(reading)
        if plan.name == supplied:
            assert field == plan.current(fresh)
            assert field.recorded_at() == 30.0
            assert field.attribution() == CurrentReadingAttribution()
        else:
            assert field == plan.current(historical)
            assert field.attribution() == LastKnownReadingAttribution((historical_owner.name,))
    assert document.observations[historical_owner] == historical
    if supplied != "model_observation":
        assert reading.current_model(owner) == UndeclaredModel()


def test_published_unknown_replaces_old_value_without_attesting_other_fields(historical_runtime: tuple[UnrecordedThreadIncarnation, AgentRuntimeInfo]) -> None:
    historical_owner, historical = historical_runtime
    owner = RecordedThreadIncarnation("current-spelling", 20.0)
    fresh = ModelObservation(model=UndeclaredModel()).project_runtime_info(
        AgentRuntimeInfo(), RuntimePublication(owner, 30.0))
    reading = RuntimeInfoDocument({historical_owner: historical, owner: fresh}).read_scope(
        RuntimeReadScope(owner, (historical_owner.name,)))
    assert reading.model == UndeclaredModel()
    assert reading.model_attribution() == CurrentReadingAttribution()
    assert reading.context_used == PiCount.from_wire(25)
    assert reading.context_size == PiCount.from_wire(100)
    assert "last known; owner unrecorded" in reading.context_reading().status_text()


def test_current_publications_keep_independent_field_clocks_through_roundtrip() -> None:
    owner = RecordedThreadIncarnation("A", 20.0)
    observed = ModelObservation(model=DeclaredModel("current/model")).project_runtime_info(
        AgentRuntimeInfo(), RuntimePublication(owner, 30.0))
    observed = ContextUsageObservation(context_used=PiCount.from_wire(25)).project_runtime_info(
        observed, RuntimePublication(owner, 31.0))
    observed = ContextSizeObservation(context_size=PiCount.from_wire(100)).project_runtime_info(
        observed, RuntimePublication(owner, 32.0))
    restored = FieldCodec.decode(RuntimeInfoDocument, FieldCodec.encode(RuntimeInfoDocument({owner: observed})))
    saved = restored.observations[owner]
    assert saved.model_observation.recorded_at() == 30.0
    assert saved.context_used_observation.recorded_at() == 31.0
    assert saved.context_size_observation.recorded_at() == 32.0
    assert saved.current_model(owner) == DeclaredModel("current/model")


def test_unattributed_runtime_retains_distinct_map_key_and_stored_thread_spelling() -> None:
    key = UnrecordedThreadIncarnation("dictionary-A")
    origin = UnattributedRuntimeOrigin(key.name, "stored-B", 12.0)
    info = AgentRuntimeInfo(model_observation=origin.capture(DeclaredModel("old/model")))
    document = FieldCodec.decode(RuntimeInfoDocument, FieldCodec.encode(RuntimeInfoDocument({key: info})))
    assert document.observations[key] == info
    current = RecordedThreadIncarnation("C", 20.0)
    reading = document.read_scope(RuntimeReadScope(current, (key.name,)))
    assert reading.model_attribution() == LastKnownReadingAttribution((key.name, "stored-B"))
    assert reading.current_model(current) == UndeclaredModel()


@pytest.mark.parametrize("historical_model,expected", [
    (UndeclaredModel(), CurrentReadingAttribution()),
    (DeclaredModel("old/model"), LastKnownReadingAttribution(("fixer",))),
])
def test_configuration_labels_the_displayed_value_not_a_historical_unknown(
    wired: Comms, historical_model: ThreadModel, expected: ReadingAttribution,
) -> None:
    thread = wired.registry.require("fixer")
    historical = UnrecordedThreadIncarnation(thread.name)
    origin = UnattributedRuntimeOrigin(thread.name, thread.name, 12.0)
    info = AgentRuntimeInfo(model_observation=origin.capture(historical_model))
    wired.agents.runtime_info.replace(RuntimeInfoDocument({historical: info}))
    answer = wired.configuration.answer(thread.name)
    assert answer.model_attribution == expected
    wired.threads.set_thread_configuration(thread, NextTurnConfiguration(model=ChosenModel("configured/model")))
    configured = wired.configuration.answer(thread.name)
    assert configured.model == "configured/model"
    assert configured.model_attribution == CurrentReadingAttribution()


def test_recorded_runtime_follows_its_birth_not_a_reused_name() -> None:
    original = RecordedThreadIncarnation("A", 20.0)
    info = ModelObservation(model=DeclaredModel("original/model")).project_runtime_info(
        AgentRuntimeInfo(), RuntimePublication(original, 30.0))
    document = RuntimeInfoDocument({original: info})
    renamed = original.renamed("C")
    reading = document.read_scope(RuntimeReadScope(renamed, ("A", "B")))
    assert reading.model == DeclaredModel("original/model")
    assert reading.current_model(renamed) == DeclaredModel("original/model")
    replacement = RecordedThreadIncarnation("A", 40.0)
    assert document.read_scope(RuntimeReadScope(replacement, ("A",))) == UnobservedRuntimeInfo()
