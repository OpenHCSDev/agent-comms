# W3: Classifiers, and durable versioned annotations

**Index:** [README.md](README.md).

## Target

**Classifiers are a family, and authority is carried by inheritance.**

```python
class SpanLabel(ABC):
    """An answer to one question about one span, from some source."""

class ModelLabel(SpanLabel):          # a classifier's answer, with its probabilities
    classifier: ClassifierVersion

class HumanLabel(ModelLabel):         # a correction; outranks every model label by being more specific
    author: ThreadIncarnation
```

The label in force for a question is the most derived one present, resolved through the class hierarchy. A correction overrides a model's answer because a human label *is a* refined model label, never because of a priority number.

**Classifier members:** `Jev` first, behind the route DW2 chooses; a second model can join later for comparison on identical questions. Each records its pinned version.

**Annotations are durable and versioned:** one typed table keyed by span (segment hash, offset, length), question, and classifier version. Unchanged text is classified once per pinned version. Changing the pin reclassifies, and both versions' answers remain queryable, so an audit can always say which model version labelled what.

## Done when

The table holds Jev's labels for rules and commitments, a human correction overrides a model label without any priority field, and re-running on unchanged text makes no request.
