# Foundation typing follow-up

The initial reusable foundation commit remains 19323fb (do not rewrite it after
publishing foundation-ready.md). This follow-up adds complete signatures and
narrow typing casts at the runtime type evaluator/registry boundary, explicitly
checks that the decoded family tag is a string, and adds reserved-tag/default/
optional-value cases. No public module, field encoding or registry API changed.

The external registry package has no py.typed marker; its import has a single
import-untyped annotation. The metaclass retains ABCMeta in its declared MRO,
so static consumers can recognize the inherited ABC contract.
