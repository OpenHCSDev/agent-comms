"""Hierarchical lazy agent settings built on ``objectstate`` and ``FieldCodec``.

``GlobalAgentSettings`` is the single declared root holding every agent configuration
dataclass group (``ThreadSettings``, ``RuleGateSettings``, and future groups) as
attributes. Each scope layer (Global -> Project -> [Channel] -> Thread) stores only
authored overrides keyed by exact dotted field paths; omitted fields stay ``None`` and
resolve lazily through ``objectstate.config_context`` + ``resolve_with_provenance``.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field, fields, replace
from functools import cache
from pathlib import Path
from typing import Annotated

from objectstate import (
    GlobalConfigBase,
    LazyDataclassFactory,
    ResolvedField,
    ScopeKey,
    config_context,
    get_base_config_type,
    resolve_with_provenance,
    set_base_config_type,
)
from pydantic import JsonValue
from python_introspect import FieldLabel
from python_introspect.validation import validate_annotated_dataclass

from agent_comms.nominal.field_codec import FieldCodec, FieldPlan
from agent_comms.registry.agent_settings_store import AgentSettingsStore
from agent_comms.registry.agent_settings_wire import (
    SCOPE_SEPARATOR,
    AgentSettingsDocument,
    AgentSettingsScope,
    ProjectSettingsScope,
    ResolvedSetting,
    ScopedSettingsProjection,
)
from agent_comms.registry.native_settings import NativeThreadDefaults
from agent_comms.registry.thread_settings import ThreadSettings
from agent_comms.storage.locked_store import DocumentChange
from agent_comms.team.rule_deal import RuleGateSettings

assert ScopeKey.SEPARATOR == SCOPE_SEPARATOR


def _default_thread_settings() -> ThreadSettings:
    return ThreadSettings(
        model=NativeThreadDefaults.model,
        thinking_level=NativeThreadDefaults.thinking_level,
    )


@dataclass(frozen=True)
class GlobalAgentSettings(GlobalConfigBase):
    """Single declared root for all agent settings groups and their global defaults."""

    thread: Annotated[ThreadSettings, FieldLabel("Thread settings")] = field(
        default_factory=_default_thread_settings
    )
    rule_gate: Annotated[RuleGateSettings, FieldLabel("Rule gate settings")] = field(
        default_factory=RuleGateSettings
    )


@dataclass(frozen=True, slots=True)
class SettingGroupSchema:
    """Introspected schema for one settings group attribute on ``GlobalAgentSettings``."""

    name: str
    concrete_type: type
    lazy_type: type
    plans: dict[str, FieldPlan]


@cache
def _group_schemas() -> tuple[SettingGroupSchema, ...]:
    defaults = GlobalAgentSettings()
    schemas: list[SettingGroupSchema] = []
    for item in fields(GlobalAgentSettings):
        concrete_type = type(getattr(defaults, item.name))
        lazy_type = LazyDataclassFactory.make_lazy_simple(concrete_type)
        plans = {plan.name: plan for plan in FieldCodec.field_plans(concrete_type)}
        schemas.append(SettingGroupSchema(item.name, concrete_type, lazy_type, plans))
    return tuple(schemas)


@cache
def _field_schema_by_path() -> dict[str, tuple[SettingGroupSchema, FieldPlan]]:
    return {
        f"{group.name}.{plan.name}": (group, plan)
        for group in _group_schemas()
        for plan in group.plans.values()
    }


def _ensure_base_config_registered() -> None:
    """Register ``GlobalAgentSettings`` only when no host root (e.g. ``ToadSettings``) is set."""
    try:
        get_base_config_type()
    except RuntimeError:
        set_base_config_type(GlobalAgentSettings)


def _decode_layer(
    scope: AgentSettingsScope,
    overrides: Mapping[str, JsonValue],
) -> tuple[object, ...]:
    """Decode one scope's authored overrides into ``objectstate`` context layer objects."""
    by_path = _field_schema_by_path()
    grouped: dict[str, dict[str, object]] = {group.name: {} for group in _group_schemas()}
    for dotted_path, raw_value in overrides.items():
        if dotted_path not in by_path:
            raise ValueError(f"Unknown agent setting path: {dotted_path!r}")
        if raw_value is None:
            continue
        group, plan = by_path[dotted_path]
        grouped[group.name][plan.name] = FieldCodec.decode(plan.annotation, raw_value)

    def build_global() -> tuple[object, ...]:
        base = GlobalAgentSettings()
        return (
            replace(
                base,
                **{
                    group.name: replace(getattr(base, group.name), **grouped[group.name])
                    for group in _group_schemas()
                    if grouped[group.name]
                },
            ),
        )

    def build_scoped() -> tuple[object, ...]:
        return tuple(
            group.lazy_type(**grouped[group.name])
            for group in _group_schemas()
        )

    return scope.build_layer(for_global=build_global, for_scoped=build_scoped)


def _canonicalize_updates(
    existing: Mapping[str, JsonValue],
    updates: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    """Validate and merge authored override updates; ``None`` clears a field back to inherit."""
    by_path = _field_schema_by_path()
    merged = dict(existing)
    base = GlobalAgentSettings()
    grouped_decoded: dict[str, dict[str, object]] = {}
    for dotted_path, raw_value in updates.items():
        if dotted_path not in by_path:
            raise ValueError(f"Unknown agent setting path: {dotted_path!r}")
        if raw_value is None:
            merged.pop(dotted_path, None)
            continue
        group, plan = by_path[dotted_path]
        decoded = FieldCodec.decode(plan.annotation, raw_value)
        grouped_decoded.setdefault(group.name, {})[plan.name] = decoded
        merged[dotted_path] = FieldCodec.encode(decoded, plan.annotation)
    for group_name, values in grouped_decoded.items():
        validate_annotated_dataclass(replace(getattr(base, group_name), **values))
    return merged


@dataclass(frozen=True)
class ResolvedAgentSettings:
    """Effective typed agent settings at one scope together with ``objectstate`` provenance."""

    scope: AgentSettingsScope
    ancestry: tuple[AgentSettingsScope, ...]
    layers: dict[str, dict[str, JsonValue]]
    resolved_fields: dict[str, ResolvedField]
    values: GlobalAgentSettings

    def to_projection(self) -> ScopedSettingsProjection:
        by_path = _field_schema_by_path()
        target_scope = self.scope.scope_id
        resolved_wire: dict[str, ResolvedSetting] = {}
        for dotted_path, resolved in self.resolved_fields.items():
            group, plan = by_path[dotted_path]
            source_scope = resolved.source_scope or ""
            source_type = (resolved.source_type or group.concrete_type).__name__
            resolved_wire[dotted_path] = ResolvedSetting(
                value=FieldCodec.encode(resolved.value, plan.annotation),
                source_scope=source_scope,
                source_type=source_type,
                authored=source_scope == target_scope,
            )
        return ScopedSettingsProjection(
            scope=target_scope,
            ancestry=tuple(layer.scope_id for layer in self.ancestry),
            layers={scope_id: dict(values) for scope_id, values in self.layers.items()},
            resolved=resolved_wire,
        )


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Resolves and persists hierarchical agent settings over ``AgentSettingsStore``."""

    store: AgentSettingsStore

    @classmethod
    def at(cls, root: Path) -> AgentSettings:
        return cls(AgentSettingsStore.at(root))

    def resolve(
        self,
        scope: AgentSettingsScope,
        *,
        channel: str = "",
        document: AgentSettingsDocument | None = None,
    ) -> ResolvedAgentSettings:
        """Resolve all settings at ``scope`` through ``objectstate.config_context``."""
        _ensure_base_config_registered()
        saved = self.store.read() if document is None else document
        chain = scope.ancestry(channel=channel)
        layers_wire: dict[str, dict[str, JsonValue]] = {}
        decoded_layers: list[tuple[ScopeKey, tuple[object, ...]]] = []
        for member in chain:
            overrides = saved.overrides_at(member.scope_id)
            layers_wire[member.scope_id] = overrides
            decoded_layers.append((ScopeKey(member.scope_id), _decode_layer(member, overrides)))

        resolved_fields: dict[str, ResolvedField] = {}
        resolved_groups: dict[str, object] = {}
        with ExitStack() as stack:
            for scope_key, layer_objects in decoded_layers:
                for layer_object in layer_objects:
                    stack.enter_context(config_context(layer_object, scope_id=str(scope_key)))
            for group in _group_schemas():
                group_values: dict[str, object] = {}
                for field_name in group.plans:
                    resolved = resolve_with_provenance(group.lazy_type, field_name)
                    resolved_fields[f"{group.name}.{field_name}"] = resolved
                    group_values[field_name] = resolved.value
                resolved_groups[group.name] = group.concrete_type(**group_values)

        return ResolvedAgentSettings(
            scope=scope,
            ancestry=chain,
            layers=layers_wire,
            resolved_fields=resolved_fields,
            values=GlobalAgentSettings(**resolved_groups),
        )

    def projection(self, scope: AgentSettingsScope, *, channel: str = "") -> ScopedSettingsProjection:
        return self.resolve(scope, channel=channel).to_projection()

    def thread_defaults_for(self, project: Path) -> ThreadSettings:
        """Effective thread creation defaults for ``project`` (Global -> Project)."""
        return self.resolve(ProjectSettingsScope(project.name)).values.thread

    def apply_rule_gate_overrides[Record: RuleGateSettings](
        self,
        record: Record,
        scope: AgentSettingsScope,
        *,
        channel: str = "",
    ) -> Record:
        """Apply only authored ``rule_gate.*`` overrides across ``scope``'s ancestry.

        Un-overridden fields stay untouched so model-specific defaults (such as CLM's
        ``OffDealMode``) remain intact unless explicitly overridden at Global, Project,
        Channel, or Thread scope.
        """
        saved = self.store.read()
        chain = scope.ancestry(channel=channel)
        authored_paths = {
            path
            for member in chain
            for path in saved.overrides_at(member.scope_id)
            if path.startswith("rule_gate.")
        }
        if not authored_paths:
            return record
        resolved = self.resolve(scope, channel=channel, document=saved)
        overrides = {
            path.removeprefix("rule_gate."): resolved.resolved_fields[path].value
            for path in authored_paths
        }
        return replace(record, **overrides)

    def update_scope(
        self,
        scope: AgentSettingsScope,
        values: Mapping[str, JsonValue],
        *,
        channel: str = "",
    ) -> ScopedSettingsProjection:
        """Persist validated authored overrides at ``scope`` and return the updated projection."""
        def change(
            document: AgentSettingsDocument,
        ) -> DocumentChange[AgentSettingsDocument, AgentSettingsDocument]:
            merged = _canonicalize_updates(document.overrides_at(scope.scope_id), values)
            updated = document.with_scope_overrides(scope.scope_id, merged)
            return DocumentChange(updated, updated)

        updated_document = self.store.update(change)
        return self.resolve(scope, channel=channel, document=updated_document).to_projection()
