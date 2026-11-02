"""Response models for the orders API."""
from pydantic import BaseModel


class OrderOut(BaseModel):
    id: int
    amount: int
    status: str
