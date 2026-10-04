# F1: typed target action owner

Core-first paired draft. Existing CliCommand owns action membership, bound target coordinates, command declaration, editable fields and confirmation. Replace dictionary bindings with partially bound command instances and describe with one typed TargetAction. FieldCodec remains the codec owner; no copied JSON schema projection. Existing OwnerStartResult remains Start result; other target command leaves return their nominal result records. CLI encodes results at its JSON boundary.

Migration covers every target binding and confirmation, target-catalog discovery, execute_target/editor arguments, Start reconnect and tool consumer, CLI output, and all paired Toad menus/slash/forms/fixtures. Original execution revalidation and acquired route/form/task lifetimes remain intact.

Read owner/all consumers and full AST first, implement/delete copies, batch checks and installed actual application last. Draft is not Ready. No native changes, providers, environments or frozen421 writes.
