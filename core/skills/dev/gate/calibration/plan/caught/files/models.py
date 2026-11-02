"""Order model."""
from dataclasses import dataclass


@dataclass
class Order:
    """One customer order."""

    id: str
    total: float
