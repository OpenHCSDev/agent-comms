# Actual installed channel-scope investigation

No product code changed. PR345 stays closed and its converter stays deleted; PR154 stays merged. This report closes the independent core/history diagnosis, not the parent's complete live UI journey.

## Verified finding

On runtime-workspace-navigation-20260929 (reported installed core942/Toad156), the same ChannelConversation and HistoryReadRequest used by CommsChatView return eight initial rows for #comms, eight for #nra, and zero for #openhcs. The correctly captured ChannelDisplayHistory also returns zero #openhcs archive rows. The parent's clicked view showed those same 8/8/0 results after its full wait. There is no captured-page versus mounted-page contradiction in this reported state.

The earlier diagnostic ChannelHistory('#openhcs', None) bypasses the channel predicate. ChannelHistory.capture supplies those targets directly; ChannelDisplayScope.includes accepts EVERY message when targets is None. Its twenty rows are unfiltered archived messages, not evidence of twenty OpenHCS messages. The earlier live archive receipt's twenty rows for that invocation did not prove scoped #openhcs history availability; source/proof preservation remains verified.

Read-only enumeration of every authoritative public row and captured registry found:

| Source | Public rows | Exact #openhcs targets | Captured #openhcs matches | OpenHCS members |
| --- | ---: | ---: | ---: | ---: |
| Actual live bus | 185 | 0 | 0 | 2 |
| source-0njqz198 | 8400 | 0 | 0 | 0 |
| source-a7v0vr4w | 20 | 0 | 0 | 0 |

The channel is exact tag openhcs with any_mode=True. That mode admits sender/DM-peer/mention activity for members captured from each source registry; none of the live rows references those members and neither archive declares them. No archived OpenHCS member/target activity exists in these supplied wire sources. This finding says nothing about separate native journals or other project roots, which were not inspected.

The installed _refresh_history awaits _mount_page before setting _history_initialized (comms_chat.py447/452). The full timeout therefore confirms an empty page; it does not establish that matching rows were dropped.

## Existing owner and next gate

ChannelConversation.page delegates to canonical HistoryViews.channel_display_page, which passes ChannelDisplayHistory(channel); that owner captures ChannelDisplayScope per historical registry. Those production owners agree with actual saved data. No new predicate, scope, audience, membership borrowing or wake policy is needed.

Parent owns correction of its live_navigation.py expectation: compare the requested channel's canonical captured page with the actual mounted page. Keep the saved-row/painter checks for #comms/#nra and all draft/Document/EditHistory/participant/native assertions. For the genuinely empty #openhcs page, verify matching empty mounted state, selected channel/root, actual composer presentation/focus and normal return; do not display unrelated unfiltered messages to satisfy an unconditional nonempty assertion. Tesla156 retains shared CommsChatView ownership; no shared-file edit was made here.

## Evidence and boundary

installed-captured-reader.json records the exact installed initial reader, captured archive predicate and unfiltered diagnostic results. Comms was constructed with publication clients disabled; an existing human identity was required before reading. Public bus/registry/metadata/checkpoint revisions across live and historical roots were unchanged. No prompt or owner restart occurred. The parent's actual UI clicks/timeout are in evidence/workspace-navigation-deployment/live_navigation.py, live-navigation-history-painted.txt and live-navigation.json in its shipping tree.

Current history data gives the expected empty page. No production defect or implementation blocker was found in this boundary. Remaining continuous clicked/native acceptance belongs to parent/Tesla/Carver/Dalton; this audit does not replace it.

Relevant ownership patterns: BOUND-2 (diagnostic bypass of the richer captured owner), IDEN-7 (unfiltered check answers a wider question), IMPL-13 (use the same canonical reader for the expected UI result). Latest NRA/refactor-audit and standing AGENTS were reread. No broad scan or matrix was rerun for unchanged product code.
