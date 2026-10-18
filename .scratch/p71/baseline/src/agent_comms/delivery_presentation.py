"""A delivery failure owns its presentation; views supply the concrete controls."""


class DeliveryPresentation:
    def present(self, receiver, body: str) -> None:
        receiver.delivery_refused(self.description, body)
