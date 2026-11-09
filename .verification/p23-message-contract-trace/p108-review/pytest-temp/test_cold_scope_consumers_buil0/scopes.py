@decorate(mark)
class Outer(base(mark)):
    @decorate(mark)
    def method(self, arg: mark = mark) -> mark:
        if condition:
            class Inner:
                def local(self): return mark
        def nested(): return mark
if condition:
    class Conditional: pass
async def final(arg=mark): return mark
class Twin: pass
class Twin: pass
