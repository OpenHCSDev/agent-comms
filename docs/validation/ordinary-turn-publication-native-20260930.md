# Ordinary native publication acceptance

Arendt's fresh run02 passed three cases in33.42seconds, exit0. Exact execution
Core d6b5c706dae20f2ee454747f62eeb1236e7a013d normally integrates consumer5d612968
and Mendel Source0b5f4fc555ca1eb8b3ea406713b8bb87a651a6dc, including proof
correction085ef784. Native614 is the reviewed immutable package; dependencies
come from the existing installed checkpoint interpreter. Core imports this exact
worktree source. This is an actual native/OwnedTurn publication check, not a
fresh wheel installation, Toad UI gate or default activation.

The existing native lifecycle fixture uses only a controlled localhost provider.
Each case runs actual OwnedTurn admission, preparation, native input-start,
tool/response events, final publication and acquired resource retirement:

- Final reply: one native Started input and one committed final send. The
  transcript contains exactly one final SentTranscript with that original reference.
- Tool progress/final: actual Bash runs between the committed non-waking
  progress notice and final reply. Only the final send joins the final native entry.
- Provider503: original input remains Started, no completed final send, only
  the existing diagnostic notice. Lease and inbox retire without replay.

All three leases settled idle and all fixture native processes exited. The
sanitized receipt is [run02.json](../../evidence/ordinary-turn-publication/run02.json).
The original failed run01 remains preserved; its native final and wire sends
were already committed and are never replayed. That run exposed a wrong-domain
comparison between request-envelope inputDigest and plaintext sent_digest.
The correction uses the original indexed NativeContextJournal proof, existing
header/input/entry lineage and exact durable StartedInput text/lease. It adds no
request decoder, guessed command kind, replay path or lifecycle authority.

## Exact completed command

Run from `/home/ts/wt/comms-original-turn-custody-20260929`. The initial existence
check prevents pytest from clearing a retained original run. For a separately
authorized fresh check, choose a new persistent run directory and log name.

```sh
if [ -e .native-publication-fixtures/run02 ]; then exit 97; fi
env \
  PI_COMPACTION_TEST_PACKAGE=/home/ts/wt/comms-native-compaction-progress-20260929/stack/.pi-native-b68dfdced9148b50/node_modules/@earendil-works/pi-coding-agent \
  AC_NATIVE_COPIED_PACKAGE=/home/ts/wt/comms-native-compaction-progress-20260929/stack/.pi-native-b68dfdced9148b50/node_modules/@earendil-works/pi-coding-agent \
  PYTHONPATH=src:/home/ts/.cache/agent-scratch/compaction-output-accounting-20260929/test-deps \
  PYTHONDONTWRITEBYTECODE=1 \
  timeout --signal=TERM --kill-after=10 110 \
  /home/ts/.local/share/agent-comms/runtime-canonical-native-checkpoint-20260929/bin/python \
  -m pytest -o addopts= -q \
  --basetemp=/home/ts/wt/comms-original-turn-custody-20260929/.native-publication-fixtures/run02 \
  tests/test_s1_event_behavior.py::test_actual_native_stream_reaches_current_consumer_and_settlement \
  > /home/ts/.cache/agent-scratch/original-turn-custody-20260929/ordinary-publication-native02.log 2>&1
```

Preflight: scoped fixture process scan found none; available RAM14.4GiB,
home28.4GiB. Swap15.6GiB raised a warning. This bounded serial check reused
installed dependencies, ran one native process at a time and made no paid calls.
No public owner, route or default runtime changed. Original run01/run02 journals,
dispositions and committed wire/proof records remain in the named private roots.

The six changed production files have zero positive debt ratchet measures
against Source4f84338f, including per-file/per-function StringDispatch,
TypeSwitch and arm counts. Consumer deletion:27lines; additions38lines close
the acquired send/source relation. The proof correction deletes15lines and
adds25lines to use the existing original native evidence. Two fake success
fixtures delete59lines; the existing actual producer fixture replaces them.

Reference-only durable TurnRouting requires the separately owned quiet
certified annotation carry before a coherent future installation. Parent owns
integration/activation; Arendt owns that carry builder and its outside-src
operator, while Mendel owns the routing semantic API. The completed installed41MB
resource journey in Toad223 is a separate gate and was not repeated. Remaining
S14 initial command custody and other lifecycle closure are not claimed done.
