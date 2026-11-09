from __future__ import annotations


class LexicalScopeContext:
    scopes = ('owned',)

    def _resolve_name(self, name):
        return name in self.scopes

    @property
    def _active_class_scope(self):
        return self.scopes[-1]


class Existing: pass
