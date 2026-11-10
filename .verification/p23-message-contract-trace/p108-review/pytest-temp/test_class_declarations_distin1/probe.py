renamed = 99
def outer():
    hidden = 3
    class Owner:
        hidden = 4
        del hidden
        copy = renamed
    return Owner
print(outer().copy)
