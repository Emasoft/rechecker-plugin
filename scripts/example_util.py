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
