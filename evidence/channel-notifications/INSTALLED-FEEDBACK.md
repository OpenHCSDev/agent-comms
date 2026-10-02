# Channel and DM feedback integration

Parent core includes merged PF1/PF2/PF5, PF4 validated route observation,
per-message assignment outcomes, existing selected activity, and recent incoming
receipts in the DM/native conversation. Toad PR103 includes parent DM receipt
commit plus PR102 feedback and PF4 observation changes.

## Actual path checks

- installed-dm-readonly.json: verified true, two production mounted DM views read
  actual original bus/coordination rows. No messages sent. Includes user messages
  77/79; PR95 responded to77 and UX is pending behind failed71.
- installed-dm-*.svg: mounted production screenshots, original source receipts.
- route-seals-receipts.log:36 passed across typed marker route, seals and receipts.
- activity-receipts-regressions.log:14 passed, including actual registry lease
  versus stale idle Activity, no Ready while a turn is active.
- Actual user messages77/79 are the current live acceptance inputs. Do not resend.

## Corrected test identity

installed-ui-route-fixed.log/report watched OLD74 because it selected the first
message with the same body. Its actual fresh send is76 (timing/source identities
corroborated in PF4 LIVE-76-DIAGNOSIS.md). PR95 completed76 with IGNORE. UX never
started74/76 because failed71 retained its execution pointer. The report is NOT
proof of a new74 attempt or of post-fix native send failure. Failed logs retained;
installed_ui_live.py now binds a unique probe and new exact message identity.
No automatic replay of uncertain71/74 is authorized.

## Remaining live defect

UX71 is active/prompt_starting with an expired lease and no confirmed terminal.
New messages77/79 remain pending. Pascal owns evidence-backed dead-attempt recovery
and preventing future released failures from blocking unrelated messages. This
is not successful or unsent proof; UNKNOWN must remain unsafe to retry. Parent
owns applying recovery and verifying the user's pending messages progress.

The user's original Toad process was never killed. New starts load installed
feedback; already loaded UI code requires reopening Toad. Core owner processes
were restarted only while idle. CI is deferred under owner instruction.
