"""Runbook checks (preconditions and verify): PromQL on the hub Prometheus, expect empty or nonempty."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable


class Prometheus:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def series(self, query: str) -> list[dict]:
        url = f"{self.base_url}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Prometheus HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc
        return data.get("data", {}).get("result", [])


def evaluate(prom: Prometheus, check: dict) -> tuple[bool, str]:
    """(passed, what was seen). A query error never passes."""
    try:
        result = prom.series(check["promql"])
    except Exception as exc:  # Prometheus down or bad query: fail closed
        return False, f"query failed: {exc}"
    seen = f"{len(result)} series" if result else "no series"
    return (not result) == (check["expect"] == "empty"), seen


def evaluate_all(prom: Prometheus, checks: list[dict]) -> tuple[bool, list[str]]:
    lines, ok = [], True
    for c in checks:
        passed, seen = evaluate(prom, c)
        ok &= passed
        lines.append(f"{'(/)' if passed else '(x)'} {c['name']}: expected {c['expect']}, got {seen}")
    return ok, lines


def window_seconds(within: str | None, default: int = 300) -> int:
    if not within:
        return default
    return int(within[:-1]) * (60 if within.endswith("m") else 1)


def wait_until_verified(prom: Prometheus, checks: list[dict], interval: int,
                        sleep: Callable[[float], None] = time.sleep,
                        clock: Callable[[], float] = time.monotonic) -> tuple[bool, list[str]]:
    """Re-evaluates every check until all pass together, or the longest `within` runs out."""
    deadline = clock() + max(window_seconds(c.get("within")) for c in checks)
    while True:
        ok, lines = evaluate_all(prom, checks)
        if ok or clock() >= deadline:
            return ok, lines
        sleep(interval)
