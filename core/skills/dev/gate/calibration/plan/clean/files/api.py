"""HTTP view of orders."""
from models import Order


def order_json(order: Order) -> dict:
    """Wire form of an order."""
    return {"id": order.id, "total": order.total}
