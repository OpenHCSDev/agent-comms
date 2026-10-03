# Declaration-owned thread and channel actions

Draft: source implementation in progress; not installed or live verified.

The existing CliCommand family owns command names, parameters, applicability,
confirmation and execution. FieldCodec supplies the existing JSON boundary.
Target queries project these declarations; native Toad menus and editors display
the projection and execute the same command as the CLI. ChannelManagement owns
tag assignment, rename, delete and saved-view deletion.

Delete the Toad ThreadAction semantic catalog and repeated operation bodies,
including pin/activity decisions. Keep UI request custody, route admission,
editor lifetime and navigation as frontend resources. No provider or public
writes in final validation: one installed private-bus menu/CLI journey covers
thread tag edit, channel tag rename/delete, built-in applicability and reopen.

Order: source/AST ownership and complete caller migration, coherent checkpoint,
then batched sanity and the affected installed native UI path. No test-first.
