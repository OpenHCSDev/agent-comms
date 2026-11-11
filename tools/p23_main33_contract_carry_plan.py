"""Retain current main's calls and records while keeping the P23 callback owners."""

import json
import subprocess

from p23_revision_owner_plan import ROOT, SRC, imported, patch, stage

reason = (
    "The package rebase uses the retained Git three-way inputs and keeps all nonconflicting main changes. "
    "Restore current main's runtime imports that were grouped with obsolete guarded imports in conflict windows; do not drop its RuntimeServer, export/history calls or annotation grant. "
    "Retain P23's original nominal callback arguments. Main renamed the inbox operation ClearQueue to ClearQueuedInputs, independently of the outbound pi_commands.ClearQueue: preserve that distinction and the existing commands namespace. "
    "Restore main's original ProjectReply declaration, which the consolidated P23 tree lacked, with its original frozen worktree field. "
    "These are integration corrections to existing main declarations and executable uses, not new guarded-import promotions or a claim that the combined graph is closed."
)
imports = [
    imported(SRC / "execution/acp.py", "from agent_comms.transport.runtime_server import RuntimeServer"),
    imported(SRC / "execution/turn_inputs.py", "from agent_comms.execution import pi_commands as commands"),
    imported(SRC / "execution/turn_inputs.py", "from agent_comms.execution.pi_commands import FollowingPrompt, InterruptSteering, SetModel, SetSteeringMode, SetThinkingLevel"),
    imported(SRC / "history_views.py", "from agent_comms.messaging.exporting import WireExportBoundary, WireExportFormat, WireExportLimit, WireExportProvenance, WireExportScope, WireTranscriptExporter"),
    imported(SRC / "history_views.py", "from agent_comms.messaging.historical_views import ChannelDisplayHistory, ChannelHistory, DMDisplayHistory, DMHistory, HistorySource"),
    imported(SRC / "history_views.py", "from agent_comms.messaging.message_notification import MessageNotification, NotificationWindow"),
    imported(SRC / "history_views.py", "from agent_comms.messaging.notification_evidence import FirstRecipient, OutcomeViewer, RecipientCursor, RecipientLimit"),
    imported(SRC / "messaging/sending.py", "from agent_comms.messaging.task_sources import AnnotationDisclosureGrant"),
    imported(SRC / "coordinated_runtime.py", "from agent_comms.pi_native_backend import PersistentPiSession"),
    imported(ROOT / "tests/test_native_refusal_recovery.py", "from agent_comms.execution import pi_events"),
    imported(ROOT / "tests/conftest.py", "from agent_comms.installer.runtime_resolution import RuntimeResolution"),
]
main = subprocess.check_output(
    ["git", "show", "origin/main:src/agent_comms/transport/serving/served_requests.py"],
    cwd=ROOT, text=True, timeout=15,
)
reply = "@dataclass(frozen=True)\nclass ProjectReply(DeclaredReply):\n    worktree: Checkout"
if reply not in main:
    raise ValueError("Current main's original ProjectReply declaration changed")
changes = [
    patch(SRC / "execution/turn_inputs.py", [
        ("@handles(ClearQueue)\n    async def clear_queue(self, item: ClearQueue, session: PromptSessionCallbacks)",
         "@handles(ClearQueuedInputs)\n    async def clear_queue(self, item: ClearQueuedInputs, session: PromptSessionCallbacks)"),
    ]),
    {"operation": "insert_before_target", "file_path": str(SRC / "transport/serving/served_requests.py"),
     "target_qualname": "CancelReply", "source": reply},
]
print(json.dumps({"stages": [stage("p23-retain-main-runtime-import-contracts", reason, imports),
                              stage("p23-preserve-main-queue-identity-and-project-reply", reason, changes)]}, indent=2))
