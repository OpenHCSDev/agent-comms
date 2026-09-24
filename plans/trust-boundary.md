# agent-comms trust boundary

Authoritative scope for what reviewers may block on. Read this before opening a security finding.

## The boundary

Everything running under this UID on this machine is **inside** the trust boundary. That includes every agent process, every worker, every child, the Pi fork, the wire, and anything invoked through `bash`.

A process with this UID can already:

- edit `src/agent_comms/*.py` directly
- read provider credentials
- replace an installed module between import and call
- write any file the wire writes
- send any message any agent could send

Therefore a finding whose exploit requires a same-UID process is **not a finding**. The capability it demonstrates is already unconditionally available. Hardening against it buys nothing and costs the release.

## Out of scope

Do not block on, and do not open reviews for:

- same-UID forgery of any kind, including tracked-history rows, claim rows, registry entries, or wire messages
- direct callability of an internal function by an in-process or same-UID caller outside its intended fencing
- "cooperative identity is not authentication" between local agents
- attacker-controlled input from any path only a same-UID process can write
- whole-root rollback, hardware loss, or filesystem-level tampering

If a finding fits here, record it as a one-line scope note and move on. Do not produce a repro, a NON-CLEAN verdict, or a hardening patch.

## In scope

Block on anything that occurs **without an adversary**. The test is whether a crash, a race, late delivery, or an ordering accident produces the failure.

- crash-atomicity: a durable record that can be half-written, or two records that must agree and can diverge across a crash
- lost updates and read-modify-write races between cooperating processes
- stale reads causing a write, including late-delivered messages acting on superseded state
- unauthorized spend: any path that issues a provider call the owner did not authorize
- runaway autonomy: automatic retry, reprompt, or resume that the owner did not request
- false success: reporting a stop, a receipt, a usage number, or a verdict that was not actually measured
- silent data loss, including dropped entries, false clears, and unreported truncation

## Priority

Unauthorized spend and runaway autonomy outrank everything else, including correctness. This project is self-funded. A bug that wastes an hour is cheaper than a loop that bills overnight.

Crash-atomicity and stale-read-causing-a-write come next, because they are the failures that motivated the coordination work in the first place.

## Bootstrap stance

The system is being bootstrapped deliberately, taking on debt to reach a usable state and then using the working platform to remove that debt.

Consequences for review:

- **Cooperative-advisory is a complete answer during bootstrap.** All participants cooperate by construction. A mechanism that prevents two cooperating agents from colliding has solved the real problem. Say it is advisory, say what it does not enforce, and pass it.
- **Default-off and isolated is shippable.** A primitive that is not wired into production sends cannot break production. Review it for what it claims, not for what an integrated version would need.
- **Do not require enforcement the design never claimed.** If a mechanism is documented as advisory, "it does not fence raw `bash` writes" is a correct scope note, not a defect.
- **Disjoint work does not block on shared work.** A finding in one slice never blocks an unrelated slice.

## Verdict vocabulary

- `CLEAN (scope: ...)` — meets what it claims, within a stated scope.
- `ADVISORY (does not enforce: ...)` — correct as cooperative arbitration, with the unenforced surface named.
- `NON-CLEAN` — reserved for in-scope failures. An adversary-requiring finding never earns this.
- `OUT OF SCOPE` — one line, no repro, no patch.

## When the boundary changes

This boundary holds while agent-comms runs locally, single-user, with all agents under one UID.

It changes if any of these become true, and only then should the out-of-scope list be revisited:

- the wire is exposed over a network
- a second user or a second UID participates
- untrusted code runs as a participating agent
- the wire ingests messages from a source outside this machine

Until one of those happens, treat this document as authoritative and cite it by name instead of re-deriving the argument.
