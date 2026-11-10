renamed = 99
class Owner:
    def method(value=(hidden := 3), other=hidden): pass
    copy = hidden
print(Owner.copy, renamed)
