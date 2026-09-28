# Paired runtime live — 2026-09-28

Installed from merged fork revisions: core66e8fa13 (273), Toad67ddc9e6 (125),
Textual16ede007; native package5fdef596596173bd. All five user launchers select
runtime-failure-recovery-candidate-20260928. Despite that historical directory
name, it is now the live installation. stack/pyproject.toml and uv.lock record
the merged source pins.

## Actual execution

- The paired installed native Pi/ACP/Toad run passed queue consumption, exact
  native attribution, cold reattachment, direct replies, active/idle channel
  status, Checked/no-response feedback and stopped-owner reopening. Its provider
  was a loopback fixture; it is not claimed as a paid-provider result.
- Quiet cutover stopped/restarted the four idle owners and reset only
  compaction-commits.sqlite3 and input_dispositions.json. Native sessions/proofs,
  coordination state and durable bus/history remained in place. No input replay.
- The operator's final check called a removed lifecycle helper AFTER completing
  the switch. That failed receipt is retained, not relabeled a pass. Independent
  checks then verified all four new processes, current runtime executables and
  owner sockets; fresh installed ACP initialize/load succeeded for every owner.
  Each native session revision remained unchanged across its attachment check.
- A file-based mounted installed UI probe loaded #comms, UX DM and PR95 DM from
  the actual route without sending a prompt. PR95 had zero wire-DM rows; native
  history was tested by ACP attachment, not inferred from that count. Earlier
  stdin probe failed multiprocessing startup and an overbroad nonempty-DM
  assertion; it is not counted as passing UI acceptance.
- The user independently reported live compaction completed in about2:40.
  Earlier actual selected-provider isolated compactions took2:27 and2:34.
- A subsequent NRA prompt reached native Started, then the provider WebSocket
  closed1011 with no answer text. The user's explicit retry worked. A separate
  authorized HTTP-streaming fork also encountered provider connection refusal;
  current transport settings remain unchanged. Its test process and39MB copy
  were removed. Dalton owns structured provider-failure feedback.

## Indexing observation

Indexing is the unread-count projection of retained native history, not agent
turn activity. Live UX index offset advanced from17839229 to22040587; another
owner index was already complete. Initial stuck-cache diagnosis was wrong:
WireRevision includes a one-second expiry tick. The speculative refresh patch
was discarded without committing or installing it. No claim about index finish
time or broad idle CPU is made.

## Remaining work

T4,116/126 resource lifetime completion,50 browser scope,274 actual native fixture
closure, original-plan qualifications and provider error feedback remain active.
This deployment does not claim the whole refactoring goal finished. CI deferred.
Executed cutover operator removed; concise successful and failed receipts kept.
