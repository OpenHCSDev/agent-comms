---
name: read-open-order-before-sendback
description: "before striking, read every open order on the problem and check that the mechanism the strike claims actually exists"
type: feedback
---

**Why:** That includes the commander's own code and orders: a wrong order is slop too; say so, undo any strike it caused, send the corrected order.
