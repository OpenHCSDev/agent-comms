"""Use the existing declared-family query in the nominal-provider regression."""

import json

from p23_revision_owner_plan import ROOT, patch, stage

print(json.dumps({"stages": [stage(
    "p23-use-original-ledger-family-membership-query",
    "The restored run collected three tests and failed only because the new regression called nonexistent LedgerAttempt.members. The native DeclaredFamily API is members_with; querying the original LedgerAttempt root enumerates its declared members without a manual roster. Keep the membership/concreteness assertions and production source unchanged.",
    [patch(ROOT / "tests/test_goal_nominal_providers.py", [
        ("for member in LedgerAttempt.members():", "for member in LedgerAttempt.members_with(LedgerAttempt):"),
    ])],
)]}, indent=2))
