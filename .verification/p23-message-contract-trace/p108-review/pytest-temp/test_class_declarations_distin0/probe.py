renamed = 99
def outer():
    hidden = 3
    class Owner:
        copy = renamed
        hidden = 4
    return Owner
print(outer().copy)
