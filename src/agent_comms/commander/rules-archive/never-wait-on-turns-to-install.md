---
name: never-wait-on-turns-to-install
description: "Installs never wait for agent turns to finish; stop every unit at once, install (~2 min), start the same set at once"
type: feedback
---

Never wait for units to "finish their turn" before an install. Stop them all in parallel, install, start the same set in parallel. A stopped unit resumes its order on start; a mid-turn stop costs almost nothing.

**Why:** 2026-10-19 the install itself took ~2 min, but waiting on idle turns (require_idle at tools/upgrade_bus.py:396) blocked installs ~2 h and left units idle ~1 h. Owner, furious: "how many times did we wait on agents to finish their turn ... I've dealt with this so many times." It recurs across sessions.

**How to apply:** until P252 (installer owns stop/start of one set, in parallel) lands, stop/start in parallel myself; never sequential loops, never wait. After P252, just run the installer. Related: [[slow-is-a-defect]], .
