# P389: ratchet timeout reproduction

Baseline: `810cde3d07d4294a6bb3ceea8b88fcab8c223edb`.

Before production changes, ran one parameter of
`tests/guards/test_debt_ratchet.py::test_net_reduction_does_not_pay_for_a_new_identifier[src/agent_comms-admission]`
in an isolated reproduction checkout, with shared Python and an outer 300-second bound.
The copy's Repository.compare timeout was 10 seconds. A controlled 30-second delay
before executing each checker module ensured the interpreter children were still
running when the comparison timed out. Neither change is a proposed production fix.

Pytest returned 1. Its identity was PID 627090, start_time 535867567.
The ratchet leader exited, but these 18 processes were still running:

- Pool workers (all reparented to PID 1): 629674, 629675, 629676, 629677,
  629678, 629679, 629681, 629682, 629684, 629685, 629687, 629688.
- Checker interpreters: 629731 (black), 629734 (ruff), 629736 (mypy/head),
  629738 (mypy/base), 629741 (black), 629746 (ruff).

All remained in the original pytest group, PGID 627090. The reproduction driver
held its identity-bound SpawnedProcess handle and retired only that owned group;
its final group inventory was empty. No process-name search or pkill was used.

Full process identities, PPIDs, states, RSS and command lines are retained in
`/home/ts/.cache/agent-scratch/unit-hygiene/p389-reproduction.log`;
pytest output is in `p389-reproduction-pytest.log` beside it.

Source ownership: SynchronousProcess owns group retirement and reaping.
SpawnedProcess owns launched synchronous handles. ParentLifeline owns detecting
parent loss. Ratchet subprocess.run calls and its uncontained ProcessPoolExecutor
bypass those owners (IMPL-13).
