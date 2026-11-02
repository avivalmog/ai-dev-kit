"""Price helpers."""


def discount(price: float, amount: float) -> float:
    """Price after subtracting `amount`, never below zero."""
    return max(price - amount, 0.0)
