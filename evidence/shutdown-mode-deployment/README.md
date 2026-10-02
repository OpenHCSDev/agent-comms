# Installed shutdown worklist ownership

Textual10 merged1738abd8, based on merged frame-damage9. Paired noneditable core6bd
and Toadc253 unchanged. Five launcher symlinks select runtime-frame-shutdown-20260928.
Existing owners, native sessions, selected settings and durable history unchanged.

The authentic earlier RuntimeError occurred when widget unmount removed an
inactive mode while shutdown awaited pruning. Shutdown now captures existing
stack references before its first await; live mode removal remains legal and
the existing destruction path clears resources. One production line changed;
no catch, skip, retry, second lifecycle store or retained destroyed state.

Exact installed native resize/mode return/unmount test passes and asserts every
destruction callback plus empty registry/screens/modes/stacks. Actual PTY test
resizes and switches modes, deliberately raises a UI IndexError, and proves
shutdown preserves that original failure while completing every destructor.
The controlled IndexError is a shutdown test stimulus; the separate frame-damage
producer fix remains installed and its actual resize receipt is retained in the
preceding deployment. No provider call or user input replay. CI deferred.
