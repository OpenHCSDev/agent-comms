# Extension CLI resolution and acceptance correction

## Actual source and parent fix review

- `extensions/pi-agent-comms/index.ts`: `run()` uses bare `agent-comms` through
  execFileSync; the factory invokes `tools` immediately. ENOENT fails startup.
- `extensions/pi-project-sync/index.ts`: the managed tool-call handler also uses
  bare `agent-comms`; it inherits the same child environment.
- Subagent prefers the current script with process.execPath. Its executable
  fallback is separate from the reported Comms CLI failure. No invocation tested.
- `OwnerLifecycle._launch_owner_unlocked` inherits service environment and chooses
  the native console script beside sys.executable. Choosing that absolute launcher
  alone does not make sibling console scripts discoverable to its Node child.
- `native_pi.main` is the appropriate native child composition boundary. Parent's
  reviewed change prepends `Path(sys.executable).parent` to the copied child PATH,
  retaining inherited PATH (os.defpath if absent), before os.execvpe. This selects
  the current installation's console tools without depending on shell activation.
  It does not resolve the Python symlink to a different system installation, alter
  global environment, or weaken package/import authority. No change requested.

Review source: parent's `comms-acp-saved-session-startup-20260928` tree. No production
file was edited by this follow-up. Parent reports installed copied-session success
with the actual owner's environment; live agent reply verification remains parent
owned and is not inferred from get_state, a user prompt, or registration output.

## Corrected harness regression

Removed the test-only CLI wrapper and source PYTHONPATH injection. An explicit
`--runtime-path` is required and used unchanged for Node resolution and child
execution. The receipt records PATH, resolved Node/Comms paths, no fixture CLI and
no source PYTHONPATH. Direct native discovery/get_state scope is explicit.

Command from this tree:

```sh
/home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python \
  stack/test-native-global-extensions.py .artifacts/package \
  .artifacts/restricted-path-regression --runtime-path /usr/local/bin:/usr/bin
```

Expected regression observed: exit **1**, `agent_comms: null`, exact
`spawnSync agent-comms ENOENT` in registration error. No session opened; kernel
network denial and zero provider prompts. See `restricted-path-regression.json`
and `.txt`. This negative regression does not claim to test the parent's wrapper
fix, which has separate installed acceptance.

Parent authorized cleanup after copying the package into stable /var/tmp. Owned
`.artifacts/package`, `startup`, `startup-final`, and `restricted-path-regression`
are removed after retaining these receipts. The published source/harness and
original session/proof in parent's tree are retained untouched.
