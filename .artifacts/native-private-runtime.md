# Private native acceptance resources

Owner: core-native-runtime. Purpose: installed P18/P40/P41 verification only.

- `.native-acceptance-wheels/`: local wheel built from checkpoint c637b37a6.
- `.native-acceptance/`: private Python installation, installed with that wheel and no dependency copies. One .pth borrows `/home/ts/wt/core-goal-retry/.venv/lib/python3.14/site-packages`; its owning checkout remains untouched.
- Frozen SDK borrowed read-only: `/home/ts/wt/core-envelope-895/.native-login-acceptance/node_modules/@earendil-works/pi-coding-agent`.

No default pointer or installed fleet changes. Retire this private runtime/wheel after acceptance and active-borrower checks. Preserve all failed/UNKNOWN inputs and original sessions.
