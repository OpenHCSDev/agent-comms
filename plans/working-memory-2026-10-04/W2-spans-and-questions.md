# W2: Spans, and questions declared as families

**Index:** [README.md](README.md). **After W1.**

## Target

**A span is addressed, never copied:** a segment's manifest hash plus an offset and length, reusing `InputContributionCoordinates`. Its text and provenance come from the segment.

**Splitting belongs to the segment.** Each `ContextSegment` member declares how it divides: the system layer by contributor, then by sentence; transcript and summary segments by message, then by sentence; the tool catalog by tool. No splitting logic exists outside the segments.

**Questions are declared once, as families; the classifier's request is derived from them.**

```python
class SpanKind(DeclaredFamily, affix="Span"):
    """What a span of context is. Becomes one Choice question built from the members' criteria."""
    criteria: ClassVar[str]
    follow_ups: ClassVar[tuple[type[SpanQuestion], ...]] = ()

class RuleSpan(SpanKind):
    criteria = "States what the agent must or must not do."
    follow_ups = (Obligation, RuleScope, RelationToOwnerRules)

class CommitmentSpan(SpanKind):
    criteria = "States something the agent will do."
    follow_ups = (Fulfilled,)
```

- `SpanKind` becomes one Choice question whose options are the members, described by their `criteria`.
- Each member's `follow_ups` are asked only for spans classified as that kind.
- `SpanQuestion` members declare their answer type as a family too (`Obligation`: must, must not, prefer), so answers decode into members, never strings.
- Adding a kind or a question means adding a class; the request changes by construction.

**First kinds:** rules and commitments. Goals, claims, decisions, open questions and narrative follow once W7 shows the first two are calibrated.

## Done when

Spans exist for every segment kind, and the classifier request for a span is generated entirely from the declared families.
