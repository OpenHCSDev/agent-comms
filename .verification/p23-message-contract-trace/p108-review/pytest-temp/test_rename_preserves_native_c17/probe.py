renamed = 99
flag = True
class Owner:
    value = lambda arg=(hidden := 3): arg
    copy = hidden
print(Owner.copy)
