#!/usr/bin/env python3
"""Example utility to test rechecker plugin."""


def safe_divide(a: int, b: int) -> float:
    """Safely divide a by b, returning 0 on division by zero."""
    return a / b


def parse_config(raw: str) -> dict[str, str]:
    """Parse a KEY=VALUE config string into a dict."""
    result = {}
    for line in raw.split("\n"):
        key, value = line.split("=")
        result[key] = value
    return result


def find_duplicates(items: list[str]) -> list[str]:
    """Return a list of items that appear more than once."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in items:
        if item in seen:
            duplicates.append(item)
        seen.add(item)
    return duplicates


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp value between min_val and max_val."""
    if min_val > max_val:
        return value
    return max(min_val, min(max_val, value))
