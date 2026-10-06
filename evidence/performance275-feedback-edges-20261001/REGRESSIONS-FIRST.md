# Immediate performance priority: three recent regressions

Tristan's latest correction, 2026-10-01: the bounded lazy-loading design worked
and was faster than vanilla. Confirm and fix these three recent regressions
first, in parallel. The broader A–E scope in OWNER-SCOPE.md waits until these
are confirmed and fixed. Preserve that scope; this changes execution order.

## 1. #254 erases scroll velocity — Heisenberg

Owner of history_anchor.py and screens/workspace.py. Every screen layout refresh,
for every history window, is wrapped in WindowRestoration.geometry. It sets
_restoring, making request() skip observe(), then calls lookahead.relocated(scroll_y)
on exit, rebasing the tracked position. User scroll travel applied through layout
is swallowed; demand never becomes fast MovingPreparation, prefetch starves, and
the frame gate freezes while bodies rebuild on demand.

Discriminator first: log observe() travel per held-PageUp key event before and
after #254. Near-zero travel while scrolling confirms it.

Fix: compensation is a translation. relocated() takes the compensation delta
and shifts the tracked position by it, preserving travel, velocity and expiry.
Apply the geometry wrapper only to layouts that compensate (anchors present,
bodies restored), as before #254.

## 2. e7dbc1c3 recounts subtrees on every height calculation — Einstein

MeasuredViewportBody.retained_widget_count must keep the body's widget count as
owned state, updated when its children change, never by walk_children on the
layout path. Coordinate exact shared-file methods with Heisenberg before edits.

Discriminator first: count walk_children calls per second in a busy channel
while idle.

## 3. #269 runs a full admission per page load — Kepler

TranscriptHistory._resource_fragment_budget repeats admission already computed
by reconciliation. Store the admitted set on DocumentViewport for that pass and
read that owned pass resource in the edge loader. Coordinate its store/read
contract with Heisenberg and its cost model with Einstein; no duplicate
admission authority or competing viewport edits.

Discriminator first: admission calls per page load.

## Acceptance and delivery

On Tristan's actual live workload: busy channel, scroll from the input box,
hold PageUp, stop mid-history and idle. Faster than the build before #254
(`8ea96a37`) on frame time and PageUp input latency, with no frozen frames
while held PageUp is under way. Report each discriminator result before its
fix, with numbers. Correlate live recording and CPU/profile for the same run.

Heisenberg owns coherent viewport integration; Einstein and Kepler have the
disjoint streams above and must obtain shared-method grants before writing.
Toad #275 owns implementation and #277 the paired installed verification.
Preserve original sessions, inputs and evidence; no public owner replay or
DISPLAY0 focus/mouse takeover. Batch related changes and run the focused
sanity batch plus the actual affected installed path, not full suites for
each small change.
