# Private native acceptance resources

Owner: core-native-runtime. Purpose: installed P18/P40/P41 verification only.

- `.native-acceptance-wheels/`: predecessor wheel retained; current exact wheel is `cb6ea3eb6/agent_comms-0.1.0-py3-none-any.whl`.
- `.native-acceptance/`: private Python installation, now installed with cb6ea3eb6 and no dependency copies. One .pth borrows `/home/ts/wt/core-goal-retry/.venv/lib/python3.14/site-packages`; its owning checkout remains untouched.
- Frozen SDK borrowed read-only: `/home/ts/wt/core-envelope-895/.native-login-acceptance/node_modules/@earendil-works/pi-coding-agent`.

- Fresh configured-provider run: `/home/ts/.cache/agent-scratch/core-native-runtime/p62-cb6ea3-20261016T1831`. This root must never be replayed. Preserve failures and uncertain inputs. Its purpose is real TRIAGE→FULL effects/reply and actual default/explicit native selection fork rows/first replies.

No default pointer or installed fleet changes. Retire this private runtime/wheel after acceptance and active-borrower checks. Preserve all failed/UNKNOWN inputs and original sessions.
