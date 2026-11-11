hidden = 99
def outer():
    hidden = 3
    class Owner:
        global hidden
        def method(self):
            return hidden
    return Owner
print(outer()().method(), hidden)
