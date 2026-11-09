# Registration / selected-participant startup repair

Apply `selected-owner-handoff.plan.json` through NRA in friction's shared tree. Source input: **7b480b21166604751b36e630fdc8a72f1484513a**, including the successful RegistrySnapshot ABC/Generic anchor. The named failing startup logs are `p23-startup-after-generic-anchor-{core,toad}.log` under friction's scratch.

**Four native-clean stages, six operations, exit 0.** Plan SHA256: **bdb6409cbe4041e0aebc8e590ceda9a518a21e43c3a0d4497a199c3a236155f0**.

`Registration.claim_fresh_session` uses only the selected participant's original mutable `owner`. It never calls SelectedParticipant behavior. Its concrete parameter annotation caused Registration to load the high participant module, Comms and the still-partial MessageBus during startup.

The cut declares `RegistryOwnerSource[Owner]` in `execution/selected/owner_source.py`, composes `RegistryOwnerSource[RegistryOwner]` on the ORIGINAL SelectedParticipant, types the Registration parameter against that capability and removes the now-unread high participant module import. The capability has no constructor, data record, property forwarding or implementation. SelectedParticipant retains its exact dataclass declarations; its stored owner remains the original RegistryOwner. The selected package initializer is empty, so loading the new capability does not load participant.py.

`selected-owner.diff` shows all product changes. Every existing method body, original field declaration, constructor call, default and capture/store procedure is untouched. In particular:

- claim_fresh_session retains the same original owner, RegistryStore editing boundary, source snapshot check, runtime acquisition and mutation of participant.owner in the original order;
- SelectedParticipant's complete acquisition, worker, lease, permission, publication and capture procedures remain in their existing owner;
- UnstartedRuntime.select_session still calls claim_fresh_session with the same actual participant;
- MroDispatch remains the first parent and its declared handlers are unchanged; the added generic ABC declares only owner and empty slots;
- there is no Protocol, substitute stored record, local import, TYPE_CHECKING guard, alias or forwarding facade.

This is **native source simulation only**, using the retained Core src/tests/tools snapshot and configuration, not a startup, C3, behavior or installation claim. No Toad declaration or caller is changed. Friction owns application/reproof on the current combined Core/Toad context and the next startup result. Do not hold this cut behind further unrelated source cleanup or another standalone suite run.

`before-source.tar.gz` and `source-manifest.json` retain the input. Generated before/projected trees remain local scratch. `author_cut.py` never applies product edits. NRA checkout used: **5981d592bd5297342ad8f1f129e6bfdd213906f0**.
