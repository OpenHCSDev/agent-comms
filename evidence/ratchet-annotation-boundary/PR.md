## Fix the existing StringSubscript measurement boundary

Noether158's four Literal annotations were counted as runtime dictionary operations in both the old packaged measure and the authoritative archive StringKeySubscript classifier. The actual seven-file history6a05d5e2→600d61d7 reproduces the old **+3** and now measures **-1**, the deleted real raw dictionary access.

The existing measure now classifies Python annotation/type-declaration syntax before counting runtime subscripts. Reuse MroDispatch and AST's declared node families; no Literal name whitelist, filename exception, new metric, denominator change, product annotation hiding or parallel registry. Initializers/defaults/decorators/function bodies retain their runtime counts, including writes. Async signatures, aliases/qualified type constructors and type-parameter/PEP695 declarations have the same boundary.

Installed CLI, real Git commits, both core/Toad roots: **6passed3.79s**. Annotation-only additions pass; four real accesses in initializer/default/decorator/body fail. Existing true-read/type-check guards remain enforced. Actual158 historical receipt and bounded own changed-file metrics show no positive delta. No global scan/new agent/CI wait.

Scope is the maintained packaged core ratchet and its behavior tests. The separately installed upstream skill archive is not modified and still has the reproduced limitation; its correction should follow this documented grammar boundary. No claimed semantic inference of legacy assignment-style type aliases or arbitrary runtime generic expressions. Parent owns packaging/live pins; no product or shared-root edits.
