"""Fetch pages for the crawler."""
import requests

USER_AGENT = "crawler/1.0"


def fetch(url: str) -> str:
    """Return the page body, or raise on a non-200 answer."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=37)
    resp.raise_for_status()
    return resp.text
