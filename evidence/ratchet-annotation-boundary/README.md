## Ready: correct the existing StringSubscript classifier

Noether158's Literal annotations were counted as raw dictionary operations in BOTH old packaged StringSubscript and authoritative archive StringKeySubscript. Actual seven-file Toad history6a05d5e2→600d61d7 reproduces **+3**, and the corrected packaged measure gives **-1**, matching deletion of the real raw read.

The final fix resolves imported declarations from the actual stdlib typing module, using existing MroDispatch and Python AST owners. Names/qualified aliases come from source imports and declaration metadata. Reassigned/parameter/nested-import/otherwise shadowed names stay conservative raw accesses. There is no Literal spelling whitelist, filename waiver, blanket annotation exclusion, extra metric, product rewrite or changed denominator. Ordinary schema["type"] inside an annotation still counts. Runtime initializers, defaults, decorators and function bodies remain counted. No inspected source is executed.

The initial draft excluded whole annotation syntax; that approach was replaced after identifying its hidden raw-read counterexample. Its historical CLI receipts remain, explicitly superseded by owner-cli.log and python311-owner-cli.log.

### Actual affected acceptance

- Noneditable installed CLI and actual Git commits, both core/Toad roots: final Python3.14 **4passed2.90s**, supported Python3.11 **4passed2.69s**. The311 warning is only missing pytest-asyncio for this synchronous CLI test environment.
- Earlier unchanged true-read/type-check guards also passed in the8-case semantic CLI set. Final family covers aliased/qualified typing, annotations/PEP695 where supported, real reads in annotation/initializer/default/decorator/body, and parameter shadowing.
- Real158 before/after sources are measured through the same installed class; only its seven changed files are read. No global scan/new fleet/CI hold.
- Changed production-file measures: zero positive delta, including chain terms/foreign absence/god excess/codecs. No annotation or source-test exemption.

Scope is the maintained packaged core ratchet. The installed upstream skill archive is not modified and retains the reproduced limitation; this receipt provides its concrete correction. This is a bounded static classification, not complete Python data-flow inference: unresolved, nested-only and conservatively shadowed imports remain counted. Parent owns package pins; no live-root/product changes.
