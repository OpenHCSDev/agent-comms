hidden = 99
class Owner:
    def bump(self):
        global hidden
        hidden += 1
        return hidden
