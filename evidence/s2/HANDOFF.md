# S2 Pi RPC / turn-session refactor

Owner: codex/refactor-s2-pi-rpc-20260927, base f60abbb.
Scope: backend RPC/session/phase/failure, ACP event boundary and manual/native
RPC readers. Parent retains coordinated_runtime.py/native_prompt_send.py,
source delivery/recovery, normal FULL tool integration. No live changes.

Work in progress:
- TurnSession replaces the implicit closure state. Baseline and relocation
  each pass 205 backend/settlement tests. Relocation is not factoring evidence.
- PiRpcChannel consolidates JSON-line decoding; nominal event classes now own
  main event behavior. Excursion declarations are replacing phase switches.
- Existing PR95 persistent child, input ID, reopen, proof, tools, steering and
  cancellation semantics remain the acceptance baseline.

Evidence: .artifacts/s2 retains all logs including failed attempts. Initial test
interpreter lacked metaclass_registry; using existing integration venv with
PYTHONPATH=src (no new installation). Baseline 205 pass. Full-context NRA scan
returned deadline_incomplete at semantic_mirror_without_descent, not a clean
scan. Exact-target transformations use an authored AST migration: new async
state ownership/control transfer is not supplied as an equivalence-proof DSL
operation. Behavioral tests are required; no native equivalence claimed.

Remaining: complete commands/correlation, failure precedence, usage ownership,
all reader consumers, structural guards/new-case tests, protocol validation,
publication. Native failure public API must remain unchanged.

Checkpoint 2:
- 278 passed / 7 optional skipped after session, event, phase, failure and shared
  decoder migration. Current typed event field and command-forwarding stages:
  205 backend/settlement pass each. PendingRequests now serves both ACP setting
  and RPC transport correlation; commands own response and forwarding behavior.
- Factored input forwarding, stats epochs, and usage into behavior owners.
- No live runtime, pins, parent delivery files or other worker's history files
  changed. Still finishing structural/new-case and actual prepared-package paths;
  draft is reviewable working implementation, not acceptance-complete yet.
