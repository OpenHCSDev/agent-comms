# Native story slice: results

- Real CLI scratch-bus journey succeeded: ordered; actual saved native tool work;
  accepted verdict; send-back with one strike and one verdict; checked operation
  delivery; identical read-only re-reads. Both stories are pasted in lifecycle.md.
- The native worker read is from a copied actual session, not authored events or
  a fixture. The original session and live bus were not changed or replayed.
- Final source CLI fleet read returned exactly its strike and send-back records.
- Final focused sanity check: **5 passed, 72 deselected**, 3.39 seconds.
- `git diff --check`: passed.

Final test command (background, one worker, persistent disposable test root):

```
PYTHONPATH=$PWD/src timeout 300 /home/ts/code/projects/agent-comms/.venv/bin/python \
  -X faulthandler -m pytest -o addopts='' -n 0 --no-cov \
  --basetemp "$PWD/evidence/phase5/pytest" \
  tests/test_commander_problem_orders.py \
  -k 'order_work or strike_time or each_verdict_kind' -q
```

One environment warning remains: pytest-timeout is absent, so its configured
`timeout` option is unknown; the explicit shell timeout bounded the run.
The earlier focused invocation passed its assertions but failed the irrelevant
whole-repository coverage floor. These are sanity checks, not phase-5 acceptance.

No new tools are claimed installed. No public channel posting or script deletion
is claimed. The native record plane still overwrites earlier orders and lacks
some original transition timestamps; reads do not invent those missing facts.
See docs/agents/commander-native-story.md for the remaining source ownership work.
