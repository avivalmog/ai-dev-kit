"""Pagination for list endpoints."""


def page(items: list, number: int, size: int) -> list:
    """Items of 1-based page `number`, `size` items per page; raises ValueError below page 1."""
    if number < 1 or size < 1:
        raise ValueError("page number and size start at 1")
    start = (number - 1) * size
    return items[start:start + size]
