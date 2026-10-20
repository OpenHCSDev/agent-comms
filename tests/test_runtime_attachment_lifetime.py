"""An unsent socket attachment ends with its real owner/root/caller lifetime."""





# The 496609b95 owner's first line: an update whose context_usage predates
# the ContextReading family. A frontend must never decode it.
_UNANNOUNCED_FIRST_LINE = {"update": {"_meta": {"agentComms": {"updates": [
    {"kind": "coordination_changed", "context_usage": {"used": 1, "size": 2}},
]}}}}
_OTHER_COMMIT = "496609b95" + "0" * 31


