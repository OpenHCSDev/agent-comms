# W4: What may leave the machine

**Index:** [README.md](README.md). **Decision:** DW1.

## Why

Classifying a span with Jev sends it to a third party. Context contains code, Tristan's messages, other agents' messages, and sometimes secrets. What leaves must be a declared decision, never a side effect of classifying.

## Target

- **A disclosure policy per segment kind,** declared on the segment family: instructions, compaction summaries and agent messages may be sent; tool output and file contents may not, by default. The policy is Tristan's to change.
- **Redaction before sending:** spans resembling credentials, tokens or keys are never sent, whatever their kind.
- **Small requests:** the span, its segment kind and source description, one neighbouring sentence, and, for the relation question only, the typed list of Tristan's rules. Never the whole context.
- **Every request is recorded** with what was sent, so disclosure itself is auditable.

## Done when

No span of a non-disclosed kind can reach the classifier, and every request sent appears in a record Tristan can inspect.
