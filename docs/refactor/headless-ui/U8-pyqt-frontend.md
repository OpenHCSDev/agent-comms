# U8: The PyQt frontend, through pyqt-reactive

**Index:** [README.md](README.md). **Later, after U6 or in its place.**

## Target

- **Settings render through `pyqt-reactive`'s forms:** T1's typed settings tree held in an `ObjectState` (DU2), rendered by `ParameterFormManager`, the form a view over the state, as `pyqt-reactive` already works.
- **Runtime views are Qt-native:** the transcript, sidebars, goal display and terminal render the core's state families with Qt widgets, bound to `CoreEventStream`.
- `PyQtFrontend` joins the family with its capabilities declared, and passes the same journeys before you use it.

## Done when

The PyQt frontend passes the core journeys, and settings edited in it and in Textual share one `ObjectState` authority.
