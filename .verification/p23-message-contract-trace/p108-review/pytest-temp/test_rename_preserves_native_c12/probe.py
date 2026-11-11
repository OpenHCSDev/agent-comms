renamed = 99
flag = False
class Owner:
    before = renamed
    hidden: int = 3
    copy = before + hidden
print(Owner.copy)
