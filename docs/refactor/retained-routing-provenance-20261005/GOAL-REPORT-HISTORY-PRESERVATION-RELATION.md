# Goal-report fact: actual replacement owner

Follow-through to the source checkpoint, after Parent's review of removal commit
`4b8dc88e76ca7f46f1e6fd0f83d61102a4ebe9ba`. This records a source relationship,
not a migration or acceptance of any historical store.

Actual Git720 already declares Goal.reported_turn. TransitionGoalAction.change
writes the reporting turn into the next goal revision. GoalHistoryEntry stores
before/after goals and owner_created_at; its state is pending, committed, aborted
or uncertain. The removal commit adds GoalHistoryEntry.reports_turn and changes
GoalAction.apply to consult any committed goal-history entry reporting that turn,
instead of Thread.last_goal_report_turn. The replacement fact owner therefore
exists in the original durable history declarations; it is not a new receipt.

Future preservation must acquire the original typed Thread/incarnation and its
actual committed goal revisions under original custody, bound to the same root
and owner_created_at. It must prove that the old report marker is represented by
the authentic committed after.reported_turn evidence and preserve the complete
original history/goal relation before strict target postimage validation. Missing,
conflicting, pending or uncertain evidence cannot authorize dropping the marker.
No entry, revision, baseline, owner role or clock may be synthesized to fill it.
Original registry, release receipts and history bytes remain immutable during
that acquisition.

One concrete acquisition hazard also belongs to this future owner: original720
GoalHistoryStore.history calls observe; observe can reconcile pending entries or
insert a baseline/observed_gap. Its constructor initializes storage. Calling that
ordinary API is not a read-only proof of original committed rows. A future stopped
preservation owner must acquire the existing durable rows without those writes;
no such new acquisition or migration method is implemented here.

NativeSchemaCarryPlan and RoutingCarryPlan still do not declare this relation.
GoalReportMemberRetirement's existing raw deletion is not preservation evidence.
RecordedRegistryProjection still cannot authorize a current live registry.
P (`bb5acfcd`) preserves the original goal/report/history declarations unchanged.
Current target old-registry refusal remains required. This locates the narrow
remaining owner obligation without claiming a legitimate postimage has been
produced, or expanding frozen stopped-routing authority.
