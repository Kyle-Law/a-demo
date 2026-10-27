"""PromQL and LogQL against the hub on C3, where every cluster's metrics and logs land (B2).
Read-only HTTP GETs; results are trimmed for the model."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .kube import truncate

MAX_SERIES = 30


def _get(url: str, params: dict, timeout: int = 30) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(full, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc


class Prometheus:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def query(self, query: str) -> str:
        data = _get(f"{self.base_url}/api/v1/query", {"query": query})
        result = data.get("data", {}).get("result", [])
        lines = [f"{len(result)} series"]
        for series in result[:MAX_SERIES]:
            value = series.get("value", [None, None])[1]
            lines.append(f"{json.dumps(series.get('metric', {}), sort_keys=True)} => {value}")
        if len(result) > MAX_SERIES:
            lines.append(f"... {len(result) - MAX_SERIES} more series not shown; narrow the query")
        return "\n".join(lines)


class Loki:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def query(self, query: str, minutes: int = 30, limit: int = 100) -> str:
        end = time.time()
        data = _get(f"{self.base_url}/loki/api/v1/query_range", {
            "query": query, "start": int((end - minutes * 60) * 1e9), "end": int(end * 1e9),
            "limit": min(limit, 500), "direction": "backward"})
        lines = []
        for stream in data.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            tag = "/".join(labels.get(k, "-") for k in ("cluster", "namespace", "pod", "container"))
            for ts, line in stream.get("values", []):
                lines.append((int(ts), f"{time.strftime('%H:%M:%S', time.gmtime(int(ts) / 1e9))} {tag} {line}"))
        lines.sort(reverse=True)
        return truncate("\n".join(l for _, l in lines) or "no log lines")
