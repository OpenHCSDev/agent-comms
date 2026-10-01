# Owner-directed Toad scrolling feedback repair

Tristan supplied these source findings and revised scope on 2026-10-01.
These are hypotheses until the listed discriminator confirms them on the actual
busy live workload. Preserve the bounded working set, dormant measured bodies,
and existing demand family. No replacement cache, state mirror or competing
viewport owner.

## Source findings

Reconciliation retriggers itself. DocumentViewport.request is subscribed to
screen_layout_refresh_signal, every layout refresh of the screen. But
reconciliation itself restores and retires bodies, which causes layout refreshes,
which call request, which sets _pending, so _reconcile's while self._pending loop
runs another full pass. Each new message in a busy channel adds another trigger
through register. Nothing distinguishes a layout change caused by your input
from one caused by the last pass, so while you sit still the loop keeps running
on the event loop, competing with painting. Discriminator: reconcile passes per
second while stationary, which should be zero once settled.

A body's cost disagrees with itself across retirement. A dormant body reports
the widget count stored when it retired, while a live one recounts its subtree.
Retirement can happen mid-prune (the code says so: children "may still be
pruning"), and streaming content changes afterwards, so a body can look cheap
while dormant, get admitted and restored, recount higher, overflow the budget,
and push another body out. Repeat, and you get your load-unload oscillation.
Discriminator: log the admitted set each pass; the same bodies flipping in and
out means this is it.

Staleness only tracks width. body_measurement_stale is set only when the width
changes. A body whose content grew while dormant restores without anchor
compensation (_restore_body only uses preserve_history when the measurement is
stale), so its real height differs from its recorded rows, everything below it
moves, and the visible set changes, which feeds defect 1. That matches the page
jumps in the footage. Discriminator: the identity and offset of the top visible
item before and after each restore.

The hot path repeats whole-tree work. Every pass walks the full tree
(body_roots), recounts live subtrees (walk_children inside retained_widget_count,
which get_content_height also calls on every height calculation), and recomputes
protected() once per owner inside the retire loop.

## Parallel streams and discriminators

Before changing code, each stream confirms its defect on Tristan's live workload:
busy channel, scroll from the input box, stop mid-history and idle. If its
discriminator does not show the defect, report that and stop that proposed fix.

### A — Reconciliation retriggers itself

Owner of widgets/viewport_body.py, DocumentViewport. request() is subscribed to
screen_layout_refresh_signal, and _reconcile's own restores and retires cause
layout refreshes, so _pending stays set and passes repeat while the user is idle;
register() adds a trigger per new message. Make reconciliation reach a fixed
point: it runs on input-driven scroll, viewport size change, new owners and the
settle timer, and ignores layout refreshes caused by its own passes.

Discriminator: reconcile passes per second while stationary; must be zero after
settling.

### B — A body's cost disagrees with itself

MeasuredViewportBody and PresentationBudget.admit. Dormant bodies report the
widget count stored at retirement, which can be mid-prune, while live bodies
recount their subtree, so admission can flip the same bodies in and out. One
body has one cost: record it where it changes (restore, content growth), use
that in admission whether dormant or live, and never recount by walking the
subtree.

Discriminator: the admitted set logged each pass; the same bodies flipping
confirms it.

### C — Staleness tracks only width

MeasuredViewportBody, DocumentViewport._restore_body, history_anchor.py. A body
whose content grew while dormant restores without anchor compensation, shifting
everything below it. Staleness covers content changes too; any restore whose
height can differ from the recorded rows goes through preserve_history, and
every compensation goes through lookahead.relocated(), never observe().

Discriminator: the top visible item's identity and offset before and after each
restore; a change confirms it.

### D — Whole-tree work on the hot path

DocumentViewport. body_roots() walks the full tree every pass,
retained_widget_count walks subtrees and is read by get_content_height on every
height calculation, and protected() is recomputed once per owner inside the
retire loop. Compute each once per pass, maintain widget counts incrementally
(B owns the cost model; coordinate), and measure with the existing paint tracing
on the live workload.

## Shared-file ownership

A and C both touch _restore_body and request(): one agent owns both, the owner
of viewport_body.py. B and D coordinate through the cost model. Nobody else
changes these files until the four streams report.

## Acceptance on the actual live workload

- Zero reconcile passes and zero retires or restores per second while stationary
  after settling.
- No movement of the top visible item across restores.
- Frame time and input latency while scrolling from the input box at least as
  good as on September 27, Toad `972ba8d3`.
- Report each stream's discriminator result first, then its fix, with before
  and after figures. Video and profile must cover the same run.
- Batch related fixes and use one focused sanity batch followed by the affected
  installed user journey. Do not run the full suite for individual small edits.

Delivery: Toad #275 owns implementation; #277 owns the paired installation.
Five-minute checkpoints produce a working change, measured live result, or an
exact blocker. Source tests and unreviewed recordings are not usability claims.

## E — Raise the ceiling: retain rendered lines in dormant bodies

Tristan added this required follow-up on 2026-10-01. It starts after A through C
report. The owner of viewport_body.py coordinates it because it changes the
same restore path. Keep the existing body and presentation ownership; do not
introduce a parallel cache authority.

Today PreparedMarkdown.retire_body removes its MarkdownBlock children, and
restore_body runs await self.update(self.source): a full markdown re-parse and
widget rebuild on the UI thread for every body scrolling back into view.
Meanwhile WorkspaceScreen._prepare_compositor_refresh withholds frames until
visible bodies are restored, so any prefetch miss freezes the screen.

Target: one state family per body, owned by the body:

- Live: widgets, interactive.
- Rendered: the body's strips cached at its measured width, painted through
  Textual's line API, read-only.
- Measured: extent only, used under memory pressure.

Retiring caches the strips before dropping the widgets. Scrolling through
dormant history paints from the cache. A body re-materializes only on interaction
(focus, selection, hover on an interactive part) or when the width changes,
which turns Rendered into Measured. The frame gate never blocks on a Rendered
body, since it always has correct lines to paint.

Discriminator, before building it: on the live workload, how often the frame
gate withholds a refresh while scrolling, and for how long.

Acceptance: no withheld frames while scrolling through Rendered bodies;
input-to-paint latency for held PageUp no worse than at the bottom of the history.
Report the discriminator first and the before/after numbers. The state family
must own transitions, strip validity and memory-pressure retirement; views
derive from it rather than holding independent flags or copied lifecycle state.

Tracking: Toad #275 carries this scope and its measured discriminator; #277
carries paired installation and live verification. If E needs a continuation
after a useful A–D checkpoint, open and cross-link its draft before long work,
then carry this scope and acceptance into that PR body.
