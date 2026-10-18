---
name: orders-carry-the-design
description: "an order states the target shape, the owner to extend and the banned slop shapes up front, because units copy the nearest code: the surrounding pattern shows what was done, not what is correct"
type: feedback
---

A model's strongest signal is the code right around its edit, so it copies that code's structure. If that structure is a roster, a forwarder or a fallback, the unit writes more of it. The new code looks consistent and passes a glance.

Consequences for the commander:
- Orders name the owner, the target shape and the banned shapes, because otherwise the template is whatever slop sits beside the edit.
- Cleanup goes in the same change, because slop left in place becomes the next unit's template.
- When judging an edit, ask whether it matches the correct shape, not whether it matches the file.
- Naming, comment density and formatting follow the file. Structure follows the correct-maintenance results.
