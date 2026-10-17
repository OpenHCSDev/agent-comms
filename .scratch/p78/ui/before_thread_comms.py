"""Owner-scoped right-sidebar communication and explicitly declared work links."""

from __future__ import annotations
from agent_comms.mro_dispatch import handles
from toad.core_event_carrier import CoreEventReceiver, CoreEventMessage
from toad.core import events as core_events

from toad.navigation_target import NavigationTarget, linked_target, ThreadTarget

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from agent_comms.display_order import ThreadSort
from agent_comms.ui_model.sidebar import ThreadRowModel
from textual import on
from textual.binding import Binding
from textual.content import Content
from textual.widgets import Checkbox, Static

from toad.core.input_events import SelectTarget
from toad.widgets.comms_sidebar import CommsRow
from toad.widgets.message_filter import MessageCategory
from toad.widgets.session_sort import SortControl, ThreadSortForm
from toad.widgets.side_bar import SideBar, SideBarCollapsible, SidebarVisibilityObserver
from toad.widgets.sidebar_tree import SidebarGroup, TargetTree
from agent_comms.relationships import RelationshipEntry, RelationshipGroup, ThreadCommsSnapshot
from toad.widgets.thread_comms_source import RelationshipSource

def entry_target(entry: RelationshipEntry) -> NavigationTarget:
    person = entry.person
    if person is None:
        return linked_target(entry.target)
    return ThreadTarget(person.thread.name)


def _update_content(widget: Static, content: Content) -> None:
    """Retain identical text and spans; polling itself is not a paint change."""
    previous = widget.content
    if not isinstance(previous, Content) or not previous.is_same(content):
        widget.update(content, layout=False)


@dataclass
class RelationshipTreeState:
    expanded: dict[type[RelationshipGroup], bool] = field(default_factory=dict)
    scroll: dict[type[RelationshipGroup], float] = field(default_factory=dict)
    selected: tuple[type[RelationshipGroup], str] | None = None
    selected_targets: tuple[tuple[type[RelationshipGroup], str], ...] = ()


class RelationshipSort(SortControl[ThreadSort]):
    def __init__(self):
        super().__init__(ThreadSortForm, "relationships")

    def update_groups(self, groups):
        orders = {group.order for group in groups}
        self.update_order(next(iter(orders)) if len(orders) == 1 else None)

    def order_writer(self):
        tree = self.query_ancestor(SideBarCollapsible).query_one(ThreadCommsSidebar)
        service, owner = tree.service, tree.owner
        snapshot = tree._snapshot
        if snapshot is None:
            raise ValueError("Relationship ordering is not available yet")
        groups = snapshot.sortable_groups

        async def write(order: type[ThreadSort]):
            try:
                for group in groups:
                    await self.app.preparation.run_thread(service.set_order, owner, type(group), order)
            finally:
                if tree.service is service and tree.owner == owner:
                    tree.refresh_relationships(force=True)

        return write


class RelationshipRow(CommsRow):
    BINDINGS = [Binding("ctrl+c", "copy_identity", "Copy name", show=False)]
    available = True

    @property
    def thread_incarnation(self):
        return self.entry.person.thread.incarnation if self.entry.person is not None else None

    def action_copy_identity(self) -> None:
        self.app.copy_to_clipboard(self.target_name)

    def action_open_selected(self) -> None:
        tree = self.query_ancestor(ThreadCommsSidebar)
        tree.remember_row(self)
        if not self.available:
            self.notify("This historical thread is unavailable. Ctrl+C copies its name.",
                        title="Comms")
            return
        tree.request_navigation(self)

    def on_click(self, event) -> None:
        if event.button == 3:
            event.stop()
            self.query_ancestor(ThreadCommsSidebar).show_row_menu(self, event.screen_offset)


class RelationshipRows(SidebarGroup[tuple[str, str], RelationshipRow]):
    DEFAULT_CSS = """
    RelationshipRows > .group-header > SidebarDisclosure { width: 1; }
    """

    def __init__(self, model: RelationshipGroup, expanded: bool):
        self.model = model
        super().__init__(Static(model.title), expanded=expanded, scrollable=False,
                         id=f"relationships-{type(model).declared_name}")

    def toggle_members(self) -> None:
        super().toggle_members()
        self.query_ancestor(ThreadCommsSidebar).view_state.expanded[type(self.model)] = self.expanded

    def members_visibility_changed(self, previous: bool, expanded: bool) -> None:
        state = self.query_ancestor(ThreadCommsSidebar).view_state
        if previous:
            state.scroll[type(self.model)] = self.member_container.scroll_y
        if expanded:
            self.member_container.scroll_to(
                y=state.scroll.get(type(self.model), 0), animate=False)

    def present(self, model: RelationshipGroup) -> None:
        self.model = model

    def sync_members(self, changed: frozenset[str] | None = None) -> None:
        """Reconcile member rows from this group's entries and the tree's derived thread rows."""
        if not self.member_container.is_attached or not self.accepts_members():
            return
        tree = self.query_ancestor(ThreadCommsSidebar)
        rows = tree.thread_rows
        container = self.member_container
        entries = {entry.identity: entry for entry in self.model.entries}
        empty = container.query_one_optional(".relationship-empty")
        if entries and empty is not None:
            empty.remove()
        if self.expanded and not entries and empty is None:
            container.mount(Static(self.model.empty_text, classes="relationship-empty"))
        # Keep the top visible row stable when newer entries reorder a
        # scrolled list. Identity, rather than list index, owns selection.
        old_scroll = container.scroll_y
        anchor = (next((row for row in container.children
                        if isinstance(row, RelationshipRow) and row.region.bottom > container.content_region.y), None)
                  if old_scroll > 0 else None)
        previous_order = tuple(container.children)

        def create(key):
            entry = entries[key]
            return RelationshipRow(entry_target(entry), entry.target)

        def update(key, row):
            entry = entries[key]
            row.entry = entry
            row.available = entry.available
            row.target = entry_target(entry)
            if not entry.available:
                row.retire_thread_row()
                row.remove_class("-busy")
                row.add_class("-wire-thread")
                _update_content(row, Content(f"? {entry.target}\n  Unavailable · Ctrl+C copies name"))
            elif entry.person is not None:
                name = entry.person.thread.name
                if changed is None or name in changed or row.thread_row is None:
                    row.show(rows[name])
            else:
                row.set_label(entry.target)
                row.tooltip = entry.target
            if entry.detail:
                row.tooltip = Content(f"{entry.target}\n{entry.detail}")

        wanted = tuple(key for key in entries if self.expanded or key in self.rows)
        ordered = self.reconcile_rows(
            wanted, create, update,
            replace=lambda key, row: row.thread_incarnation != (
                entries[key].person.thread.incarnation if entries[key].person is not None else None))
        tree.apply_selection()
        if ordered != previous_order:
            if old_scroll > 0 and anchor in ordered:
                new_y = sum(2 if row.has_class("-wire-thread") else 1
                            for row in ordered[:ordered.index(anchor)])
                container.scroll_to(y=new_y, animate=False, immediate=True)


class ThreadCommsSidebar(CoreEventReceiver, SidebarVisibilityObserver, TargetTree):
    DEFAULT_CSS = """
    ThreadCommsSidebar { height: auto; }
    ThreadCommsSidebar .relationship-context { height: auto; text-wrap: nowrap; text-overflow: clip; color: $text-muted; }
    ThreadCommsSidebar .relationship-empty { height: 1; color: $text-muted; }
    ThreadCommsSidebar > Checkbox { height: 1; border: none; padding: 0; margin: 0; background: transparent; }
    """
    BINDINGS = [("up", "cursor_up", "Previous"), ("down", "cursor_down", "Next"),
                ("enter", "open_selected", "Open")]

    def __init__(self, owner: str, *, wire_root: str | None = None,
                 source: RelationshipSource | None = None, live: bool = False):
        super().__init__()
        self.owner = owner
        self.wire_root = wire_root
        self._cursor = 0
        self._generation = 0
        self._refresh_task = None
        self._revision = None
        self._source = source
        self._live = live
        self._filter_view = None
        self._filter_controls: dict[Checkbox, type[MessageCategory]] = {}
        self._snapshot: ThreadCommsSnapshot | None = None
        self._states: dict[tuple[str | None, str], RelationshipTreeState] = {}
        self.groups: dict[type[RelationshipGroup], RelationshipRows] = {}
        # Thread rows for this snapshot's people, keyed by thread name.
        self.thread_rows: dict[str, ThreadRowModel] = {}

    @property
    def view_state(self):
        return self._states.setdefault((self.wire_root, self.owner), RelationshipTreeState())

    @property
    def service(self):
        if self._source is None:
            raise ValueError("Relationship source is not connected")
        return self._source

    def compose(self):
        yield Static("Connecting…", classes="relationship-context", markup=False)
        self._filter_controls.clear()
        for category in MessageCategory.members_with(MessageCategory):
            checkbox = Checkbox(category.label, id=f"filter-{category.declared_name}",
                                classes="message-filter", compact=True)
            self._filter_controls[checkbox] = category
            yield checkbox

    def on_mount(self):
        # Use the left roster's existing observation cadence; no extra timers
        # per group or per mounted thread view.
        self.observe_core(self.app.coordination_access.events)
        self.observe_core(self.app.events)
        if self._live:
            self._bind_screen_identity()
        self._sync_filter_control()
        self.refresh_relationships()

    def sidebar_visibility_changed(self) -> None:
        """Busy rows repaint from the app's spinner clock; a shown panel refreshes on_show."""

    def on_show(self):
        if not self.is_attached or not self.screen.is_current:
            return
        # Background tabs retain their source and per-view filter state. Apply
        # the latest owner/selection once they become visible rather than doing
        # seven checkbox queries on every global update in every hidden tab.
        if self._live:
            self._bind_screen_identity()
        self.sync_current()
        self._sync_filter_control()
        self.refresh_relationships()

    def on_unmount(self):
        self._generation += 1
        if self._refresh_task is not None:
            self._refresh_task.cancel()

    def capture_state(self) -> None:
        state = self.view_state
        for group in self.groups.values():
            state.expanded[type(group.model)] = group.expanded
            state.scroll[type(group.model)] = group.member_container.scroll_y

    def set_identity(self, owner: str, wire_root: str | None, *,
                     source: RelationshipSource | None = None):
        if self._live and source is None:
            if (owner, wire_root) == (self.owner, self.wire_root) and self._source is not None:
                return
            if wire_root is not None:
                from toad.widgets.thread_comms_source import WireRelationshipSource

                source = WireRelationshipSource(wire_root, self.app.coordination_access.observed_service)
        if (owner, wire_root) == (self.owner, self.wire_root) and source is self._source:
            return
        self.capture_state()
        self._generation += 1
        self.owner, self.wire_root = owner, wire_root
        self._revision = None
        self._source = source
        self._snapshot = None
        # Old-root rows are hidden immediately, before a new query can finish.
        for group in self.groups.values():
            group.display = False
        self.refresh_relationships(force=True)

    @handles(core_events.ThreadRowStateChanged)
    async def thread_row_state_changed(self, event: CoreEventMessage) -> None:
        """Pending actions and session state decorate rows; they are not a relationship read."""
        if self.is_attached and self.screen.is_current:
            for group in self.groups.values():
                group.sync_members()

    @handles(core_events.OpenTabsChanged, core_events.SessionSelected, core_events.CoordinationObserved)
    async def _observed(self, event: CoreEventMessage) -> None:
        if not self.is_attached or not self.screen.is_current:
            return
        if not self.query_ancestor(SideBar).presentation_visible:
            return
        if self._live:
            self._bind_screen_identity()
        self._sync_filter_control()
        self.refresh_relationships()

    def _sync_filter_control(self):
        from toad.screens.session_view import SessionView
        from toad.widgets.conversation import Conversation

        view = self.query_ancestor(SessionView).query_one_optional(Conversation)
        checkboxes = tuple(self.query(Checkbox))
        for checkbox in checkboxes:
            checkbox.display = view is not None
        if view is not None:
            if self._filter_view is not view:
                self._filter_view = view
                self.watch(view, "visible_categories", self._sync_filter_control)
            with self.prevent(Checkbox.Changed):
                for checkbox in checkboxes:
                    checkbox.value = self._filter_controls[checkbox] in view.visible_categories

    @on(Checkbox.Changed, ".message-filter")
    def filter_changed(self, event):
        event.stop()
        if self._filter_view is not None:
            category = self._filter_controls.get(event.checkbox)
            if category is not None:
                selected = self._filter_view.visible_categories
                self._filter_view.visible_categories = (
                    selected | {category} if event.value else selected - {category}
                )

    def _bind_screen_identity(self):
        from toad.screens.session_view import SessionView
        self.set_identity(*self.query_ancestor(SessionView).relationship_context())

    def _visible(self):
        return (self.is_attached and self.screen.is_current
                and self in self.screen._compositor.visible_widgets
                and not self.query_ancestor(SideBarCollapsible).collapsed)

    def refresh_relationships(self, *, force=False):
        if force:
            self._revision = None
        if not self.is_mounted or not self._visible():
            return
        context = self.query_one(".relationship-context", Static)
        if not self.owner or not self.wire_root:
            _update_content(context, Content("Waiting for thread identity"))
            return
        if self._source is None:
            _update_content(context, Content("Relationship source not connected"))
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(self._refresh(self._generation))

    async def _refresh(self, generation):
        try:
            service, owner = self.service, self.owner
            revision = await asyncio.to_thread(service.revision)
            if revision == self._revision:
                return
            snapshot = await asyncio.to_thread(service.snapshot, owner)
            # Visibility admits new reads; parking does not revoke work already
            # owned by this retained panel. Identity changes and unmount do.
            if generation != self._generation or not self.is_attached:
                return
            if snapshot.owner != owner or Path(snapshot.root).resolve() != Path(self.wire_root).resolve():
                raise ValueError("Relationship snapshot does not match this thread and wire")
            if self._snapshot is None and self.groups:
                # Owner/root replacement is a new source lifetime. Do not
                # adopt same-named native rows from the hidden previous tree.
                await self.remove_children(list(self.groups.values()))
                self.groups.clear()
                if generation != self._generation or not self.is_attached:
                    return
            models = snapshot.groups_by_type
            absent = self.groups.keys() - models.keys()
            if absent:
                await self.remove_children([self.groups.pop(key) for key in absent])
                if generation != self._generation or not self.is_attached:
                    return
            observed = self.app.coordination_access.observation
            unread = (observed.sidebar.snapshot
                      if observed is not None and observed.sidebar.snapshot is not None
                      and Path(self.wire_root).resolve() == observed.service.root.resolve()
                      else None)
            people = {entry.person.thread.name: entry.person for model in snapshot.groups
                      for entry in model.entries if entry.person is not None}

            def derive() -> tuple[dict[str, ThreadRowModel], int]:
                # ThreadView presentation may inspect process identity: off the UI thread.
                rows = {name: ThreadRowModel.of(person, unread) for name, person in people.items()}
                widest = Content(f"For @{snapshot.owner} · recent window").cell_length + 4
                for model in snapshot.groups:
                    for entry in model.entries:
                        widest = max(widest, Content(entry.target).cell_length + 8)
                        if entry.detail:
                            widest = max(widest, Content(entry.detail).cell_length + 8)
                for row in rows.values():
                    widest = max(widest, Content(row.label).cell_length + 8,
                                 Content(row.summary).cell_length + 8)
                return rows, min(widest, 512)

            thread_rows, width = await self.app.preparation.run_thread(derive)
            if generation != self._generation or not self.is_attached:
                return
            # Each group owns its prepared rows and native member fence.
            # Waiting for those workers/mounts must not freeze unrelated paint.
            context = self.query_one(".relationship-context", Static)
            _update_content(context, Content.assemble((f"For @{snapshot.owner}", "bold"),
                " · recent window" if snapshot.history_limited else ""))
            context.tooltip = snapshot.incoming_basis + f"\nLatest {snapshot.history_messages} wire messages"
            control = self.query_ancestor(SideBarCollapsible).query_one_optional(RelationshipSort)
            if control is not None:
                control.update_groups(snapshot.sortable_groups)
            # From here to the end nothing awaits: rows, groups and models change together.
            self.thread_rows = thread_rows
            for identity, model in models.items():
                group = self.groups.get(identity)
                if group is None:
                    # A new group reconciles its members once composed (on_mount).
                    group = self.groups[identity] = RelationshipRows(
                        model, self.view_state.expanded.get(identity, True))
                    self.mount(group)
                group.display = True
                group.expanded = self.view_state.expanded.get(identity, True)
                group.present(model)
                group.sync_members()
            panel = self.query_ancestor(SideBarCollapsible)
            panel.styles.min_width = width
            self._snapshot, self._revision = snapshot, revision
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError) as error:
            if generation == self._generation and self.is_attached:
                context = self.query_one(".relationship-context", Static)
                _update_content(context, Content("Comms unavailable"))
                context.tooltip = str(error)

    def _ordered_rows(self):
        return [row for group in self.groups.values() for row in group.navigation_rows]

    @property
    def selection_state(self):
        return self.view_state

    def selection_for(self, row):
        return type(row.query_ancestor(RelationshipRows).model), row.target_name

    @property
    def navigation_root(self):
        return Path(self.wire_root).expanduser().resolve() if self.wire_root is not None else None

    def open_target(self, target: NavigationTarget):
        if self.wire_root is None or Path(self.wire_root).resolve() != self.app.coordination_access.service.root.resolve():
            self.notify("This view uses a different wire; open its matching connection to navigate.",
                        title="Comms", severity="warning")
            return
        self.publish_core(SelectTarget(target))

    def request_navigation(self, row, *, entry=None, generation=None):
        if not row.is_attached:
            return
        expected = row.entry if entry is None else entry
        observed_generation = self._generation if generation is None else generation
        group = type(row.query_ancestor(RelationshipRows).model)
        self.run_worker(self._navigate_current(group, expected, observed_generation))

    async def _navigate_current(self, group_key, expected, generation):
        if generation != self._generation or not expected.available:
            return
        owner, service = self.owner, self.service
        try:
            snapshot = await asyncio.to_thread(service.snapshot, owner)
            if (generation != self._generation or not self.is_attached
                    or not self.screen.is_current):
                return
            if snapshot.owner != owner or Path(snapshot.root).resolve() != Path(self.wire_root).resolve():
                raise ValueError("Relationship snapshot does not match this thread and wire")
            current = snapshot.current_entry(group_key, expected)
            if current is None:
                self.notify("This relationship changed; refresh the list before opening it.", title="Comms")
                self.refresh_relationships(force=True)
                return
            self.open_target(entry_target(current))
        except (OSError, ValueError) as error:
            self.notify(str(error), title="Comms target unavailable", severity="error")

    def show_row_menu(self, row, offset):
        from toad.widgets.comms_menu import show_target_menu

        target, entry, generation = row.target_name, row.entry, self._generation

        def open_selected():
            # Menus hold an identity, not a row index that live sorting or
            # root/owner rebinding can change before the user activates it.
            if generation == self._generation and row.is_attached and row.available:
                self.request_navigation(row, entry=entry, generation=generation)

        items = [("open", "Open")] if row.available else []
        items.append(("copy", "Copy name"))
        show_target_menu(self.screen, offset, target, items,
                         {"open": open_selected,
                          "copy": lambda: self.app.copy_to_clipboard(target)})
