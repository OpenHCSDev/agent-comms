renamed = 99
flag = True
class Owner:
    async def method(value=(hidden := 3)): pass
    copy = hidden
print(Owner.copy)
