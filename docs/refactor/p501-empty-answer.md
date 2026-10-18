# P501: empty model completions are not unavailable runtimes

## Source ownership

`TrackedTerminal` owns the final-response state. Its append factory now makes
`EmptyAnswerTrackedTerminal` for empty output, so `CompletedTrackedTerminal`
contains the nonempty case and `matches` no longer tests text truthiness.
An unanswered notice can acquire an empty completion. An already-proved original
nonempty answer remains preserved across background notices, including empty ones.

`NativeEmptyAnswer` owns the public message, "The model finished with no text".
It is not a `NativePiUnavailable`. Their shared `NativeRequestFailure` parent owns
request-failure presentation and the default absence of an RPC refusal. Request
publication, inbox failure handling and owned-turn settlement consume that parent;
the availability/recovery catches in `backend.py`, `attempt_recovery.py` and
`coordinated_runtime.py` still catch only `NativePiUnavailable`.

Verified completion admits child reuse through the existing transport capability
before requiring answer text. A missing/ambiguous native terminal still revokes
reuse in the existing unavailable handler. Empty output does not revoke it.
Existing per-input tool-socket and outstanding-input retirement rules still apply.
No retry, restart queue, second failure store or empty-success path was added.

## Before/after source evidence

The full Core refactor-audit AST overlay before editing had zero parse errors.
Its files, the original trace and the output reconstruction are under
`/home/ts/.cache/agent-scratch/core-runtime-location/p501/`.

Relevant searches:

```
rg -n 'class .*TrackedTerminal|def (append|matches|notification|require_response)' src/agent_comms/tracked_turn.py
rg -n '\.terminal\s*=|\.terminal\.(append|tool_round|notification|later_input|keep_response)' src/agent_comms
rg -n 'NativeRequestFailure|NativeEmptyAnswer|except NativePiUnavailable' src/agent_comms/{native_pi,selected_request,drain_failures,owned_turn,backend,attempt_recovery,coordinated_runtime}.py
```

Before: empty output lived inside `CompletedTrackedTerminal("")`; its truthiness
check downgraded it to pending on the first following notice. Afterwards: one empty
terminal declaration, one empty-error declaration, no `bool(self.text)` check.
The three failure-policy consumers use the shared request-failure parent rather
than repeating a list of error classes. Availability-only consumers are unchanged.
The AST overlay is source evidence, not behavioral proof; dynamic callback and
MRO behavior are checked separately below.

## Checks and limits

- 17 focused tests passed: existing tracked-native retry tests and empty-answer
  tests covering state, recovery separation, reusable-child admission and the
  selected-request failure-publication boundary.
- Pure output reconstruction reads the original copied session without modifying
  it: all six empty final records plus 100 notice signals end in
  `EmptyAnswerTrackedTerminal`, raising `NativeEmptyAnswer`. It does not instantiate
  an input handler, native runtime, provider, RPC transport or Comms controller.
- The three existing localhost-native lifecycle cases were run. All failed before
  the prompt entered Pi: the installed extension's Core CLI rejects the newer
  registry field `Thread.turn_record`. The error is `NativePiInputNotSent`, not an
  empty-answer result. Therefore these runs do not demonstrate P501 on a running
  native child. The fixture failures and original input records are preserved.
- No input was sent to the copied or live saved sessions, no uncertain input was
  replayed, and no runtime was installed or restarted.

The PR reports the final debt-ratchet result separately against its committed head.
