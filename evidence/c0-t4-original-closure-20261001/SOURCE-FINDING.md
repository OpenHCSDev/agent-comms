# Original C0/T4 source closure, 2026-10-01

Snapshots: Core9954cdd73dff20e590aebf15467a170f0a69e799; Toad545d8c0fdaa29be6a6d03bc3f5a70d0ae8036afa. Source analysis only; no new test, package build, native process or user input. Original requirements: parent432 docs/refactor/cleanup-20260929/14-C0-SITE-LEDGER.md and C0-enforce-polymorphism.md; Toad docs/refactor/T4-god-classes.md.

## Concrete remaining unowned production decision

The original T4 plan's final App closure explicitly leaves the file-link broker unchanged. Current App._broker_event (app.py:633-670) and ConversationMarkdown.on_markdown_link_clicked (conversation_markdown.py:303-332) independently decode the SAME internal project link. Pattern IMPL-5/BOUND-8, with BOUND-2 at the historical-root consumer.

- Two producers: LinkOpenTokenRule.resolve:141-151 and PathTokenRule.resolve:170-193 encode direct paths with urllib.parse.quote and encode deferred basenames as toad-file-search.
- Left-click direct decoder: ConversationMarkdown:307-308 unquote converts the encoded path back to its original filesystem spelling.
- Right-click direct decoder: App:643-644 constructs Path from the raw suffix without unquote. Therefore a producer path containing a space, #, %, or non-ASCII name is copied with its encoded spelling. This follows directly from producer/consumer source; no failing test is needed to decide ownership.
- Left-click root: ProjectPathOwner.containing(self).project_root at303-306.
- Right-click search root: default_namespace.screen.project_path at650-652. HistoricalSessions owns project_root by its selected HistoricalThread.worktree (historical_sessions.py:50-53); it has no project_path declaration. WorkspaceScreen also has no project_path declaration. SessionView independently supplies its correct project_root. The right-click path bypasses the exact existing root capability.
- App imports private Markdown _unique_project_file/_file_lookup_notice; the root must know another renderer's search outcome grammar. Both entry paths reconstruct scheme/payload and independently choose lookup/error behavior.

## Existing-owner and consumer census

Exact source searches: git grep for toad-file:, toad-file-search:, _linked_file, _searchable_basename, _unique_project_file, _file_lookup_notice, ProjectPathOwner; and all production class declarations containing Path/File/Link/Target. These produce two URI producers and two activation/copy consumers; the search/notice helpers have no other production consumers. No existing project URI/link target family is declared.

Existing richer owners to preserve:

| Existing declaration | Authority | Required consumer |
| --- | --- | --- |
| ProjectPathOwner (project_path_owner.py:8) | project_root from the containing selected live/historical view | Both native click routes; parser captures this immutable root for worker use |
| ProjectTokenRule / LinkOpenTokenRule / PathTokenRule | Markdown token grammar/render registration | Project-link emission must use the same link declaration as admission |
| SessionNavigation.preview (session_navigation.py:197) | preview identity, admission and return/navigation | Opening an admitted file |
| Clipboard (clipboard.py) / App.copy_to_clipboard | native copy transport | Copying the admitted filesystem path |
| FileKind | file presentation / media | Preview renderer, unchanged |
| Textual native action parser/event broker | @click action syntax and native event routing | Decode the external link action at that boundary, without a second action grammar |
| NavigationTarget | thread/channel/history destinations | Unchanged; do not create a parallel workspace/navigation registry |

## Bounded independent implementation proposal

Einstein owns the project-file-link boundary closure. Extend the existing ProjectPathOwner root capability as the admission entry point. One declared project-link behavior family should own the existing internal direct-file/deferred-basename encodings and resolution/effects; this is genuinely missing behavior, not another root/path/navigation authority. Reuse existing DeclaredFamily/Command mechanisms and native parser; do not create a second registry/cache/policy. Root selection stays on original ProjectPathOwner; actual preview/copy stays on original navigation/clipboard owners.

Migrate both producers and both consumers in the same change. Delete duplicated URI-prefix branches, App's raw private helper imports and screen.project_path reconstruction. Preserve web/file/relative external URI contracts and search ambiguity/bounds. Shared algorithm lives on its original ancestor; new link behavior requires its declaration, not App/Markdown branches. Before authoring types, reread existing owner methods and captured-root/parser worker semantics.

Claim requested directly from Heisenberg for only App._broker_event, Markdown project-link emission/activation and ProjectPathOwner. No viewport/publication/read/preparation, Conversation/runtime, lifecycle, compaction, plural or retained-task consumer methods. Kepler asked for the existing real file-link pilot. Draft PR before sustained implementation; one focused sanity batch and affected installed native click/copy/history journey at the END. No implementation or usability claim yet.

## Original C0/T4 rows already superseded

All five original seals still declared: FieldCodec(field_codec.py:185), PendingRequests(pending_requests.py:21), ReadLedger(read_ledger.py:36), PiRpcChannel(pi_rpc.py:26), ChildProcess(child_process.py:781). Dispatch measures StringDispatch/TypeSwitch and arm measures remain in original packaged debt_ratchet.py:179-203; measurement membership derives Measure.members_with. No broad census rerun.

Core original family rows have current declarations GoalMentionBinding, MaintenancePhase, RelationshipEdit, OwnerLifecycleControl, RestartEnvironment/RestartRefusal, PiPayload/PiContent/PiMessage/PiResponseData and shared PiStopReason. Lifecycle/selection489, plural490, compaction499, retained481 own their active followthrough; no competing patch proposed. ImportFormat.read still delegates ImportAdapter.registry (importing.py:64-65); no new suffix parser proposed.

Toad NativeAction/DeclaredWidgetActions own declared action dispatch; ConversationAction/QuestionAction own availability/effects, not the original name switches. Original T4 declarations remain ConversationTurn/TurnOwner, ContentNavigation/BlockCursor, AgentProcess, TabOrder, Clipboard, AgentPresentation, LiveOutput/OutputStream, TranscriptPresentation. Their existing active viewport/read/lifecycle requirements remain with named owners.

Bounded AST source dimensions (three original named classes only): ToadApp443 span lines/34 methods; Conversation1565/125; ACP Agent393/44. These are descriptive spans, not semantic complexity or completion proof. Original App1803/Conversation2943/Agent1523 dispatch spans have been substantially reduced. Remaining size alone does not justify another extraction. The concrete file-link disagreement above supplies the independent decision/deletion scope.

The C0 Markdown token-rule registry row is correctly superseded by ProjectTokenRule/native MarkdownIt.add_render_rule. That accepted token-render closure did not close the two later project-link consumers, so it must not be interpreted as full T4 file-link completion.
