"""One-shot OFF restoration at the original all-stopped batch seam.

Import this member into the parent's existing retained batch operator. There is
no public execute CLI, secondary restart loop, native call or runtime migration.
"""
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import subprocess

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.pi_vocabulary import OffThinkingLevel, ThinkingLevel
from agent_comms.thread_identity import ThreadIncarnation


@dataclass(frozen=True)
class OriginalOffSelection:
    incarnation: ThreadIncarnation
    model: str | None

    def require_current(self, thread):
        if thread.incarnation != self.incarnation or thread.model != self.model:
            raise RelationViolationError('Original thinking identity/model changed')


@dataclass(frozen=True)
class ReviewedCurrentThinking:
    original: OriginalOffSelection
    thinking: type[ThinkingLevel]

    @property
    def name(self):
        return self.original.incarnation.name

    def require_current(self, thread):
        self.original.require_current(thread)
        if thread.thinking_level is not self.thinking:
            raise RelationViolationError('Reviewed current thinking selection changed')


@dataclass(frozen=True)
class RestoreOriginalThinking(StoppedOwnerInstallation):
    root: Path
    root_id: str
    audience: tuple[OwnerRestartSelection, ...]
    settings: tuple[ReviewedCurrentThinking, ...]

    def restart(self, lifecycle, request):
        if lifecycle.root != self.root:
            raise RelationViolationError('Thinking restoration belongs to another root')
        return super().restart(lifecycle, request)

    def require_selection(self, snapshot, owners):
        from agent_comms.wire_log import WireLog

        if WireLog(self.root / 'bus.jsonl').read_metadata_unlocked(required=True).root_id != self.root_id:
            raise RelationViolationError('Reviewed thinking root identity changed')
        live = {thread.name for thread in snapshot.threads.values()
                if thread.role.executable and snapshot.statuses[thread.name].active
                and thread.process_alive}
        if {owner.name for owner in owners} != live:
            raise RelationViolationError('Thinking restoration requires the complete live batch')
        if live != {selection.name for selection in self.audience}:
            raise RelationViolationError('Reviewed owner audience changed')
        for selection in self.audience:
            selection.require_current(snapshot)
        for setting in self.settings:
            setting.require_current(snapshot.threads[setting.name])

    def after_stopped(self, lifecycle):
        if lifecycle.root != self.root:
            raise RelationViolationError('Thinking restoration belongs to another root')
        if lifecycle.bus.log._private_marker_unlocked().root_id != self.root_id:
            raise RelationViolationError('Thinking restoration root identity changed')
        with lifecycle.registry.store.editing() as edit:
            snapshot = edit.document.snapshot()
            # All original processes/identities must still be the retained batch.
            # Post-stop admission counters belong to the existing handoff owner;
            # never reconstruct them from the pre-fence generation.
            for selection in self.audience:
                thread = snapshot.threads[selection.name]
                if thread.incarnation != selection.identity.incarnation:
                    raise RelationViolationError('Stopped original incarnation changed')
                thread.require_local_process(selection.process)
                thread.require_idle()
                snapshot.statuses[selection.name].require_stopped()
                if thread.process_alive:
                    raise RelationViolationError('Original process survived retirement')
            for setting in self.settings:
                setting.require_current(snapshot.threads[setting.name])
            # Complete all guards before changing even an in-memory member.
            for setting in self.settings:
                thread = edit.document.threads[setting.name]
                edit.document.threads[setting.name] = replace(
                    thread, thinking_level=OffThinkingLevel)
            edit.commit()


def prepare_off_restoration(comms, *, original_python: Path, preimage: Path,
                            expected_sha256: str, reviewed: dict[str, str]):
    """Prepare read-only from an explicitly reviewed name→current-effort set.

    Equality does not prove user intent. The parent approves this exact audience
    and current values; this factory never infers repair eligibility from history.
    Any differing current selection refuses rather than overriding a user edit.
    """
    if not reviewed:
        raise ValueError('Explicit reviewed restoration audience required')
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    result = subprocess.run([
        str(original_python), str(Path(__file__).with_name('read_thinking_preimage.py')),
        str(preimage), expected_sha256,
    ], input=json.dumps(tuple(reviewed)), env=environment, capture_output=True,
        text=True, check=True)
    originals = FieldCodec.decode(list[OriginalOffSelection], json.loads(result.stdout))
    if [original.incarnation.name for original in originals] != list(reviewed):
        raise RelationViolationError('Original settings audience changed')
    settings = tuple(ReviewedCurrentThinking(original, ThinkingLevel.decode(reviewed[
        original.incarnation.name])) for original in originals)
    snapshot = comms.registry.snapshot()
    for setting in settings:
        setting.require_current(snapshot.threads[setting.name])
    audience = tuple(OwnerRestartSelection.capture(snapshot, thread.name)
                     for thread in snapshot.threads.values()
                     if thread.role.executable and snapshot.statuses[thread.name].active
                     and thread.process_alive)
    marker = comms.bus.log.read_metadata_unlocked(required=True)
    return RestoreOriginalThinking(comms.root, marker.root_id, audience, settings)
