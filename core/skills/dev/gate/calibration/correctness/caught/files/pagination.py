"""Pagination for list endpoints."""


def page(items: list, number: int, size: int) -> list:
    """Items of 1-based page `number`, `size` items per page."""
    start = number * size
    return items[start:start + size]
