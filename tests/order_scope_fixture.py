"""One declared parent for fixtures exercising checked Todo behavior."""
from agent_comms.change_report import FileLine
from agent_comms.todos import FixScope, FixShape, Todo

FIX_SCOPE = FixScope(FixShape(parents=(FileLine.class_declaration(Todo),), members=()))
