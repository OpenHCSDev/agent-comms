from typing import get_type_hints
renamed = str
class Owner:
    hidden = int
    def method[T](self, value: hidden): pass
print(get_type_hints(Owner.method)['value'].__name__, renamed.__name__)
