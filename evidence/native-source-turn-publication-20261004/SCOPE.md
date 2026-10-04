# Native source publication keeps the original admitted turn

Original #638 installed01 retry diagnostics reject registry_goal after the user
retries a blocked goal during a different, already STARTED native turn. The
AgentInfo observation publishes the source of that same exact lease; it does not
admit another input. RegistryAdmissionCheck already owns identity/process/
admission/session/configuration and exact turn lease without goal authority.
GoalRegistryAdmissionCheck remains the selected send/goal/context authority.

Close RegistryOwner -> RegistryDocument.prepare_native_source ->
Registration.attach_native_session -> OwnedTurn/TurnProgress as one family.
Keep all source identity/lease/admission checks; retain the current document's
goal/status/phase. No refreshed authorization, native replay or disposition edit.
Use existing atomic source-owner controls and the retained original retry facts;
final changed installed retry/source path follows coherent implementation.
No worktree/environment/native/provider/public mutation is created here.
