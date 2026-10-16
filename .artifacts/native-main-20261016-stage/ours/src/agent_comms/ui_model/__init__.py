"""Frontend-neutral comms UI semantics.

Comms meaning stays in Core; this package turns it into presentation models
that any backend (Textual today, PyQt later) renders. Models own typed rows,
report what changed as keyed change sets, and flush once per frame through a
scheduler the backend supplies. Nothing here imports a UI toolkit.
"""
