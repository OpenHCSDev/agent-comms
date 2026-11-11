renamed = 99
def outer():
    hidden = 3
    class Owner:
        copy = hidden
    return Owner
print(outer().copy)
