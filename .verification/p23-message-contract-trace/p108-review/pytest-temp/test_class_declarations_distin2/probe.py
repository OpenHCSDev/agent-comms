renamed = 99
def outer():
    hidden = 3
    class Owner:
        hidden: int
        copy = renamed
    return Owner
print(outer().copy)
