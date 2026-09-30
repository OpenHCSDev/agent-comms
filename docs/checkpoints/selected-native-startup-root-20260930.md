# Selected library startup root

Owner: Arendt. Bounded follow-up after merged443 f124db8b. The original live R1
worker already supplies the correct launch root. This continuation closes the
selected library caller that supplies its explicit maintenance_root without an
ambient AGENT_COMMS_ROOT. NativeStartupAdmission.for_launch remains the sole
factory; the tracked launch passes its existing original root to that factory.

Verify only that concrete library call and acquired startup lease. Do not repeat
the accepted native startup, CPU, cancellation or whole-turn cohorts. No change
to readiness budgets, durable input dispositions, historical UNKNOWN, retry or
native protocol. The full C3 phased cutover remains442's independent priority.

## Bounded concrete acceptance

Existing test_native_startup_actual.py::
test_actual_tracked_waiting_for_startup_cancels_without_prompt passes with
AGENT_COMMS_ROOT removed and all original explicit-root startup slots held.
The actual tracked library caller uses the original maintenance_root factory,
waits on those original leases and cancels before native prompt admission.
Saved native bytes are unchanged and provider posts remain zero. One selected
check passed in0.97s using original installed000 dependencies and candidate
source; no startup/CPU/full-turn cohort repeated and no public action.

Production change: five added lines across the existing factory and caller.
The existing test gains the explicit absent-ambient-root witness. NativeStartup
policy, all original live R1 dispositions and merged443 readiness are unchanged.

Command uses installed runtime-canonical-source-publication-20260930/bin/python,
PYTHONPATH pointing to this worktree's src/tests, pytest_asyncio.plugin,
the e36 prepared package, isolated .native-init-fixtures/root-library/run01,
and a15-second outer deadline. It is a concrete acquisition/cancellation check,
not a new installed full workflow claim. Pattern IDEN-3: original root owns lease
placement; no second root resolver, ambient state mirror or deadline increase.
