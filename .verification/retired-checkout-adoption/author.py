import json
from pathlib import Path
root=Path.cwd(); ops=[]
def patch(file,target,old,new):
    ops.append(dict(operation='patch_target',file_path=str(root/file),target_qualname=target,replacements=[dict(old_source=old,new_source=new)]))
def insert(file,target,source):
    ops.append(dict(operation='insert_before_target',file_path=str(root/file),target_qualname=target,source=source))
f='src/agent_comms/registry/thread_storage.py'
patch(f,'StorageRoots','    def __post_init__(self) -> None:', '''    adopted_checkouts: Annotated[tuple[SelectedCheckout, ...], WireOmitDefault()] = ()
    released_checkouts: Annotated[tuple[StoragePath, ...], WireOmitDefault()] = ()

    def __post_init__(self) -> None:''')
insert(f,'StorageRoots.relocated','''    def adopting(self, repository: RepositoryName, path: Path) -> StorageRoots:
        return replace(self.choosing(repository, path), adopted_checkouts=(
            *(claim for claim in self.adopted_checkouts if claim.path != path),
            SelectedCheckout(repository, path)),
            released_checkouts=tuple(old for old in self.released_checkouts if old != path))

    def releasing(self, path: Path) -> StorageRoots:
        return replace(self,
            selected_checkouts=tuple(choice for choice in self.selected_checkouts if choice.path != path),
            adopted_checkouts=tuple(claim for claim in self.adopted_checkouts if claim.path != path),
            released_checkouts=tuple(dict.fromkeys((*self.released_checkouts, path))))''')
patch(f,'StorageRoots.relocated', '''            for choice in self.selected_checkouts))''','''            for choice in self.selected_checkouts), adopted_checkouts=tuple(
            replace(claim, path=destination) if claim.path == source else claim
            for claim in self.adopted_checkouts))''')
f='src/agent_comms/projects/storage_rules.py'
patch(f,'CheckoutAllocation.for_path','''        allocated = rule.roots_of(owner).allocation_name''','''        roots = rule.roots_of(owner)
        if path in roots.released_checkouts:
            return
        allocated = roots.allocation_name''')
insert(f,'ProblemCheckoutAllocation','''@dataclass(frozen=True)
class AdoptedCheckoutAllocation(EditableCheckoutAllocation):
    """An existing tree whose retired owner handed this incarnation its grant."""

    repository: RepositoryName

    @classmethod
    def read(cls, owner: Thread, path: Path, problem: str,
             suffix: str) -> Iterator[CheckoutAllocation]:
        for claim in owner.storage_roots.stored().adopted_checkouts:
            if claim.path == path:
                yield cls(claim.repository)

    def owns_declared(self, rule: StorageRule, owner: Thread, path: Path,
                      repository: RepositoryName, reads: RepositoryReads) -> bool:
        return self.repository == repository

    def owns_present(self, rule: StorageRule, owner: Thread, path: Path,
                     reads: RepositoryReads) -> bool:
        return reads.repository(path).github == self.repository


''')
patch(f,'StorageRule.checkout_paths','''        return tuple(path for path in (owner.project_path,
                                       *sorted(ordered.parent.glob(ordered.name)))''','''        return tuple(path for path in dict.fromkeys((owner.project_path,
                                       *sorted(ordered.parent.glob(ordered.name)),
                                       *(claim.path for claim in self.roots_of(owner).adopted_checkouts)))''')
f='src/agent_comms/registry/thread_status.py'
insert(f,'ThreadStatus.require_stopped','''    def require_checkout_adoption(self) -> None:
        raise RelationViolationError("Checkout adoption requires an archived source owner")
''')
insert(f,'ArchivedThreadStatus.is_archived','''    def require_checkout_adoption(self) -> None:
        """Only retired declarations can surrender their checkout grants."""
''')
f='src/agent_comms/registry/registration.py'
insert(f,'Registration.transfer_checkout','''    def adopt_checkout(
        self, original: Thread, source: Path, repository: Repo,
        require_target: Callable[[], None],
    ) -> Path:
        """Hand a retired owner's exact allocation to a live holder without moving either."""
        with self.store.editing() as edit:
            current = edit.document.require(original.name)
            if current.incarnation != original.incarnation:
                raise RelationViolationError("Checkout recipient changed before adoption")
            require_target()
            if repository not in edit.document.team_project.require().repos:
                raise RelationViolationError("Registered repository changed before adoption")
            rule, reads = self.storage_rule, RepositoryReads()
            rule.require_physical_allocation(source)
            if source == repository.checkout.resolve() or not (source / '.git').is_file():
                raise RelationViolationError("Only an isolated linked checkout can be adopted")
            owners = tuple(thread for thread in edit.document.threads.values()
                           if rule.owns_present_checkout(thread, source, reads))
            if len(owners) != 1:
                raise RelationViolationError("Checkout needs exactly one current allocation owner")
            previous, = owners
            if previous.incarnation == current.incarnation:
                raise RelationViolationError("Recipient already owns this checkout")
            edit.document.status(previous.name).require_checkout_adoption()
            if previous.process_alive:
                raise RelationViolationError("Checkout source owner still has a live process")
            rule.require_transfer_source(previous, source, edit.document.threads.values(), repository)
            require_target()
            # Both declarations commit together. Neither session nor project path changes.
            edit.document.threads[previous.name] = replace(previous,
                storage_roots=rule.roots_of(previous).releasing(source))
            edit.document.threads[current.name] = replace(current,
                storage_roots=rule.roots_of(current).adopting(repository.github, source))
            edit.commit()
        return source
''')
f='src/agent_comms/cli_verbs/cli_commands.py'
insert(f,'TransferCheckoutOperation','''class AdoptCheckoutOperation(CheckoutOperation):
    """Take over one archived owner's retained tree, keeping both session paths intact."""

    @classmethod
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: Comms, thread: Thread) -> CheckoutResult:
        source, = command.source_checkout
        source = source.expanduser().absolute()
        grant = command.grant(ctx, thread)
        requested = command.source(ctx, thread)
        repositories = ctx.registry.snapshot().team_project.require().repos
        matching = tuple(repo for repo in repositories if repo.github == requested.github)
        if len(matching) != 1:
            raise ValueError(f'{requested.github} is not one unambiguous registered repository')
        repository, = matching
        grant.require_transfer(ctx, repository)
        return CheckoutResult(ctx.registry.adopt_checkout(
            thread, source, repository, lambda: grant.require_transfer(ctx, repository)))


''')
Path('.verification/retired-checkout-adoption/adopt.plan.json').write_text(json.dumps({'recipes':[{'recipe_id':'adopt-retired-allocation-in-place','operations':ops}]},indent=2))
