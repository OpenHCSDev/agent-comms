"""Todo rows gain the declared finish forecast, initially unreviewed."""

from .carry import TodoColumnCarry


class OrderPrediction(TodoColumnCarry):
    column = "predicted_finish"


if __name__ == "__main__":
    OrderPrediction.main()
