"""Make the existing rollback regression check the runtime its root actually owns."""

import json

from p23_revision_owner_plan import ROOT, patch, source, stage

path = ROOT / "tests/test_carry.py"
name = "test_a_rollback_holds_the_root_until_every_store_opens_under_its_runtime"
before = source(path, name)
if before.count("staged.require_opens(comms.root)") != 2:
    raise ValueError("The rollback regression's two original opening checks changed")
after = before.replace(
    "            hold = backup.restore(partial(staged.restore_stores, hold))\n",
    "            hold = backup.restore(partial(staged.restore_stores, hold))\n"
    "            outgoing = replace(staged, runtime=RootRuntimeStore.at(comms.root).read().runtime())\n",
    1,
).replace("staged.require_opens(comms.root)", "outgoing.require_opens(comms.root)")
print(json.dumps({"stages": [stage(
    "p23-rollback-regression-checks-the-owning-outgoing-runtime",
    "The original unchanged regression fails with the presentation source hidden. Production Stack.restore_stores at installer/upgrade.py:669-679 reads RootRuntimeStore and reconciles using that outgoing runtime. The test's docstring requires the outgoing runtime but both opening assertions use staged's different target interpreter. Select the original runtime through RootRuntimeStore for those existing assertions; keep the complete store-opening refusal, intentional short goal ledger, no-owner-start assertion, draining hold assertion and all production code unchanged. The stored and target bus declarations may legitimately differ. Retain the failing source-hidden receipt and prove both the valid outgoing opening and intentional corrupted-ledger refusal after this correction.",
    [patch(path, [(before, after)])],
)]}, indent=2))
