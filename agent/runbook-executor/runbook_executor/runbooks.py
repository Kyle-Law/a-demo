"""Runbooks from the runbooks repo: fetch a commit, validate, match a ticket, fill in templates.

Only commits of the protected `main` are loaded (CLAUDE.md decision 7), and a plan names the
commit it was approved at, so the executor runs exactly what a human approved even if `main`
moves on. Validation is the runbooks repo's own (tools/validate.py) with that commit's schema; an
invalid runbook is skipped, never run."""

from __future__ import annotations

import io
import json
import re
import tarfile
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

import jsonschema
import yaml

TEMPLATE_VARS = {"cluster", "namespace", "kind", "name"}  # filled from the ticket's alert contract
TEMPLATE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
# Ticket values end up in PromQL strings, label selectors and API paths, so they must be plain
# Kubernetes names: no quotes, braces, slashes or spaces.
SAFE_VALUE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _http_get(url: str, accept: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "a-demo-runbook-executor"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


@dataclass
class Runbook:
    id: str
    doc: dict
    sha: str


@dataclass
class Snapshot:
    sha: str
    runbooks: dict[str, Runbook] = field(default_factory=dict)
    invalid: dict[str, list[str]] = field(default_factory=dict)  # file -> problems


def problems(doc, filename: str, raw: str, schema: dict) -> list[str]:
    """Same checks as the runbooks repo's tools/validate.py."""
    found = [f"{'/'.join(map(str, e.absolute_path)) or '(top)'}: {e.message}"
             for e in jsonschema.Draft202012Validator(schema).iter_errors(doc)]
    if found:
        return found
    stem = filename.rsplit("/", 1)[-1].removesuffix(".yaml")
    if doc["id"] != stem:
        found.append(f"id '{doc['id']}' must equal the file name '{stem}'")
    for var in sorted(set(TEMPLATE_RE.findall(raw)) - TEMPLATE_VARS):
        found.append(f"unknown template variable {{{{{var}}}}}")
    if "resource" in doc["matches"]:
        try:
            re.compile(doc["matches"]["resource"])
        except re.error as exc:
            found.append(f"matches.resource is not a valid regex: {exc}")
    return found


def parse_tarball(sha: str, data: bytes) -> Snapshot:
    """runbooks/*.yaml and schema/runbook.schema.json from a GitHub tarball, read in memory."""
    files: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            path = member.name.split("/", 1)[-1]  # drop the "<repo>-<sha>/" prefix
            if path == "schema/runbook.schema.json" or re.fullmatch(r"runbooks/[^/]+\.yaml", path):
                files[path] = tar.extractfile(member).read().decode()
    snap = Snapshot(sha)
    if "schema/runbook.schema.json" not in files:
        snap.invalid["schema/runbook.schema.json"] = ["missing: no runbook can be validated"]
        return snap
    schema = json.loads(files.pop("schema/runbook.schema.json"))
    for path, raw in sorted(files.items()):
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            snap.invalid[path] = [f"not valid YAML: {exc}"]
            continue
        errs = problems(doc, path, raw, schema)
        if not errs and doc["id"] in snap.runbooks:
            errs = [f"duplicate id '{doc['id']}'"]
        if errs:
            snap.invalid[path] = errs
        else:
            snap.runbooks[doc["id"]] = Runbook(doc["id"], doc, sha)
    return snap


class RunbookSource:
    """The runbooks at the head of `ref`, refreshed on demand, plus any commit a plan pinned."""

    def __init__(self, repo: str, ref: str, get: Callable[[str, str], bytes] = _http_get):
        self.repo, self.ref, self._get = repo, ref, get
        self._cache: dict[str, Snapshot] = {}
        self.head: Snapshot | None = None

    def refresh(self) -> Snapshot:
        sha = self._get(f"https://api.github.com/repos/{self.repo}/commits/{self.ref}",
                        "application/vnd.github.sha").decode().strip()
        self.head = self.at(sha)
        return self.head

    def at(self, sha: str) -> Snapshot:
        if not SHA_RE.match(sha):
            raise ValueError(f"not a commit sha: {sha!r}")
        if sha not in self._cache:
            data = self._get(f"https://codeload.github.com/{self.repo}/tar.gz/{sha}", "application/gzip")
            self._cache[sha] = parse_tarball(sha, data)
        return self._cache[sha]


def ticket_context(alert: dict) -> dict:
    """{cluster, namespace, kind, name, signal, resource, runbook_hint} from an alert-bridge ALERT_JSON
    block. Raises ValueError if a value isn't a plain Kubernetes name."""
    t = alert.get("ticket") or {}
    kind, _, name = (t.get("resource") or "").partition("/")
    ctx = {"cluster": t.get("cluster") or "", "namespace": t.get("namespace") or "", "kind": kind, "name": name}
    bad = [k for k, v in ctx.items() if not SAFE_VALUE.match(v)]
    if bad:
        raise ValueError(f"ticket fields not usable in a runbook: {', '.join(f'{k}={ctx[k]!r}' for k in bad)}")
    return ctx | {"signal": t.get("signal") or "", "resource": t.get("resource") or "",
                  "runbook_hint": t.get("runbook_hint") or ""}


def matches(rb: Runbook, ctx: dict) -> bool:
    m = rb.doc["matches"]
    return (ctx["signal"] in m["signals"]
            and ("resource" not in m or re.search(m["resource"], ctx["resource"]) is not None)
            and ("clusters" not in m or ctx["cluster"] in m["clusters"]))


def select(snap: Snapshot, ctx: dict) -> tuple[Runbook | None, str]:
    """The runbook for this ticket, or None and why. The alert's runbook_hint breaks a tie between
    matching runbooks but never selects one that doesn't match."""
    found = [rb for rb in snap.runbooks.values() if matches(rb, ctx)]
    if not found:
        return None, f"no runbook at {snap.sha[:7]} matches {ctx['signal']} on {ctx['resource']} in {ctx['cluster']}"
    if len(found) == 1:
        return found[0], "the only runbook that matches"
    hinted = [rb for rb in found if rb.id == ctx["runbook_hint"]]
    if hinted:
        return hinted[0], "the alert's runbook_hint among several matches"
    return None, f"{len(found)} runbooks match ({', '.join(sorted(rb.id for rb in found))}) and the alert names none of them"


def render(obj, ctx: dict):
    """Fills {{cluster}}, {{namespace}}, {{kind}} and {{name}} in every string of a runbook part."""
    if isinstance(obj, str):
        return TEMPLATE_RE.sub(lambda m: ctx[m.group(1)], obj)
    if isinstance(obj, list):
        return [render(v, ctx) for v in obj]
    if isinstance(obj, dict):
        return {k: render(v, ctx) for k, v in obj.items()}
    return obj
