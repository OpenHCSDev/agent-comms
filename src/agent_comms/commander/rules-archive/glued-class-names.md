---
name: glued-class-names
description: A class name gluing an existing domain type to a generic concept (StepField..., FunctionFieldDriver) signals a cross product flattened into names; question whether the class should exist
type: feedback
---
When a class name is an existing domain type glued to a generic concept (`StepField...`, `FunctionFieldNavigationDriver`, `PipelineXManager`), stop and ask whether the class should exist at all, before asking whether it is well written.

**Why:** the owner spotted `StepFieldNavigationDriver` from its name alone in an agent's status line (2026-10-12): "Step already exists. If there's a StepField, is there a PipelineField? and FunctionField? and what's Field? probably attribute, which is just on an object. So it's all kinds of wrong." The name implies a grid (domain type x mechanism), one class per cell; that is the row-column family of paper 1 Thm 4.5 flattened into names. The prediction held: `FunctionFieldNavigationDriver` already existed in pyqt-reactive, and the agent copied the pattern. I had first judged it "mostly right" because it extended an existing family; the right question was whether the family itself was the right shape.

**How to apply:** in review and in agent briefs: a generic mechanism (navigation, validation, preview, codec) takes the object or its declaration and asks it for its fields; it does not get a subclass per domain type. If the per-type answer is real behaviour, it lives on the domain type (or a mixin it inherits), not in a parallel `<Type><Mechanism>` class. Scan names as *candidates* only (a glued name is a reason to look, not proof). Also: do not judge a new class only by "it uses the existing family"; check the family. See [[plain-names]], , [[optimize-for-slop-elimination]].
