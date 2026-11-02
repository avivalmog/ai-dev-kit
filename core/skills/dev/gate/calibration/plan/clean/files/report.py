"""Monthly report."""
from models import Order


def monthly_total(orders: list[Order]) -> float:
    """Sum of order totals."""
    return sum(o.total for o in orders)
