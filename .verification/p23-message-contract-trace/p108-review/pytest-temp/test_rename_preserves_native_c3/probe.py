renamed = 99
flag = False
class Owner:
    value = lambda arg=(hidden := 3): arg
    copy = hidden
print(Owner.copy)
