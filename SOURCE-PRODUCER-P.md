# Amended producer P

Base: genuine Git `72062939239b0f07707406309305a056a23f925f`.
This branch is a newly amended producer, not that exact source or its retained
`c3e413` wheel, and not an original historical installed artifact. No build or
installed qualification has been performed for P.

The original lifecycle now calls installed restart phases. Thread owns restart
eligibility, Registration commits the complete idle fence atomically, and
OwnerRestartSelection captures the actual stopped owner and admission generations.
OwnerLaunch owns exact process/interpreter/environment capture; the resident queue
uses it and no longer reads `/proc` independently or owns another environment type.
The obsolete external prompt queue and its obsolete-API controls are removed;
the existing resident restart queue remains the sole queue mechanism.

The original `store_files._store_lock` still yields an integer. Its opened lock
file, WireLog durability barrier, unlock/last-close semantics and all ordinary
consumers are unchanged. OwnerLifecycle.restart_wire retains that acquisition
and yields the same integer FD to admission, stopped custody and subprocess
transport. There is no StoreLock retrofit, alternate lock or object/int fallback.
The paired current lifecycle extracts its descriptor from its own typed StoreLock.
Both phases own an integer wire_descriptor and share byte-identical phase, launch
and cutover declarations. Original fstat/flock/root checks remain in accept().

Thread fields, RegistryDocument, FieldCodec, thread_identity, store_files and
WireLog are unchanged from original720. ThreadStatus adds require_stopped behavior
to its existing family; it adds no stored field. The ordinary restart API retains
its original `agent_bin="pi"` default. Queue and phase callers use the declared
runtime policy and original captured arguments/environment.

Three authored in-memory witness/codec controls pass on P. Initial source failures
and the paired receipt are retained in the current implementation checkout.
No bus/root, writer, stop/start, native package, provider, installed prefix or
central-batch control was acquired by those controls. Actual OFD transport,
retirement, launch and unchanged-source recovery remain unqualified.

The original last_goal_report_turn field and goal-action writer remain untouched.
No carry or decoder is authorized to erase that fact. A current-format target
still requires a separately reviewed, preserving registry postimage; native
schema carry and routing carry do not supply it.
