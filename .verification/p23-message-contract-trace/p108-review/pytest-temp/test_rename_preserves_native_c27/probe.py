renamed = 99
flag = True
class Owner:
    class Inner((hidden := object)): pass
    copy = hidden.__name__
print(Owner.copy)
