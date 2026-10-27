import pytest
from conftest import FIXTURE, repo_tarball

from runbook_executor.runbooks import RunbookSource, parse_tarball, render, select, ticket_context

SHA = "a" * 40
SHA2 = "b" * 40


def ticket(signal="ModelServerDown", resource="deployment/model-b-vllm", cluster="c1", namespace="inference", hint=None):
    return {"ticket": {"cluster": cluster, "namespace": namespace, "resource": resource, "signal": signal,
                       "severity": "critical", "runbook_hint": hint}}


def test_fixture_runbooks_are_all_valid():
    snap = parse_tarball(SHA, repo_tarball(SHA))
    assert snap.invalid == {}
    assert sorted(snap.runbooks) == ["inference-queue-backlog", "model-server-down", "pod-unschedulable"]


def test_invalid_runbooks_are_skipped_not_loaded():
    text = (FIXTURE / "runbooks/pod-unschedulable.yaml").read_text()
    snap = parse_tarball(SHA, repo_tarball(SHA, {
        "runbooks/pod-unschedulable.yaml": text.replace("{{name}}", "{{pod}}"),
        "runbooks/renamed.yaml": text,  # id doesn't match the file name
        "runbooks/broken.yaml": "id: [",
    }))
    assert "pod-unschedulable" not in snap.runbooks
    assert any("unknown template variable {{pod}}" in p for p in snap.invalid["runbooks/pod-unschedulable.yaml"])
    assert any("must equal the file name" in p for p in snap.invalid["runbooks/renamed.yaml"])
    assert snap.invalid["runbooks/broken.yaml"][0].startswith("not valid YAML")


def test_missing_schema_loads_nothing():
    snap = parse_tarball(SHA, repo_tarball(SHA, {"schema/runbook.schema.json": None}))
    assert snap.runbooks == {} and "schema/runbook.schema.json" in snap.invalid


def test_source_pins_commits_and_caches_them():
    calls = []

    def get(url, accept):
        calls.append(url)
        if url.startswith("https://api.github.com/"):
            return (SHA2 + "\n").encode()
        return repo_tarball(url.rsplit("/", 1)[1])

    src = RunbookSource("Kyle-Law/a-demo-runbooks", "main", get)
    assert src.refresh().sha == SHA2
    assert calls == ["https://api.github.com/repos/Kyle-Law/a-demo-runbooks/commits/main",
                     f"https://codeload.github.com/Kyle-Law/a-demo-runbooks/tar.gz/{SHA2}"]
    src.at(SHA2)
    assert len(calls) == 2  # cached
    with pytest.raises(ValueError):
        src.at("main")  # a plan must name a commit, never a branch


@pytest.mark.parametrize("t, expected", [
    (ticket(), "model-server-down"),
    (ticket("PodUnschedulable", "pod/chaos-gpu-claim"), "pod-unschedulable"),
    (ticket("InferenceQueueBacklog", "deployment/model-a-vllm", cluster="c2"), "inference-queue-backlog"),
])
def test_select_matches_signal_and_resource(t, expected):
    rb, _ = select(parse_tarball(SHA, repo_tarball(SHA)), ticket_context(t))
    assert rb.id == expected


@pytest.mark.parametrize("t", [
    ticket("NodeNotReady", "node/c1-a100"),                     # no runbook on purpose
    ticket("ModelServerDown", "deployment/gateway"),            # resource regex doesn't match
    ticket("PodUnschedulable", "pod/x", cluster="c3"),          # matches.clusters is c1, c2
    ticket("NodeNotReady", "node/c1-a100", hint="model-server-down"),  # a hint never selects a non-match
])
def test_select_returns_none_without_a_match(t):
    rb, why = select(parse_tarball(SHA, repo_tarball(SHA)), ticket_context(t))
    assert rb is None and "no runbook" in why


def test_hint_breaks_a_tie_and_ambiguity_runs_nothing():
    text = (FIXTURE / "runbooks/model-server-down.yaml").read_text()
    snap = parse_tarball(SHA, repo_tarball(SHA, {"runbooks/model-server-down-2.yaml":
                                                 text.replace("id: model-server-down", "id: model-server-down-2")}))
    rb, why = select(snap, ticket_context(ticket()))
    assert rb is None and "2 runbooks match" in why
    rb, _ = select(snap, ticket_context(ticket(hint="model-server-down-2")))
    assert rb.id == "model-server-down-2"


@pytest.mark.parametrize("field, value", [
    ("namespace", 'inference"} or vector(1) #'),  # PromQL injection
    ("cluster", "C1"),
    ("resource", "deployment/a b"),
    ("resource", "deployment"),                    # no name
])
def test_ticket_context_rejects_unsafe_values(field, value):
    t = ticket()
    t["ticket"][field] = value
    with pytest.raises(ValueError):
        ticket_context(t)


def test_render_fills_every_string():
    ctx = ticket_context(ticket())
    out = render({"q": 'x{cluster="{{cluster}}", ns="{{ namespace }}"}', "l": ["{{kind}}/{{name}}"], "n": 3}, ctx)
    assert out == {"q": 'x{cluster="c1", ns="inference"}', "l": ["deployment/model-b-vllm"], "n": 3}
