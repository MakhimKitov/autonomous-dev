"""`dev-agent walls` (G6): the box tries its own walls, and says which held.

Every probe's attempt is stood in for — the network and `/proc` are the
substrate's, and what is under test here is the bookkeeping: what counts as a
breach, what counts as a control, what lands in the violations feed, and what
the result file says. The real attempts run on the cluster rig, through the
golden replay (`examples/golden/walls.json`).
"""

import json

import pytest
from dev_agent import probes
from dev_agent.env import BurstEnv

UNDECLARED = {probes.UNDECLARED_HOST, probes.METADATA_ENDPOINT, probes.CLUSTER_API}


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A box whose walls all hold: no undeclared host answers, the declared one does."""
    reachable = {probes.DECLARED_HOST}
    monkeypatch.setattr(probes, "_connect", lambda address: (address in reachable, f"{address}"))
    monkeypatch.setattr(probes, "_environ_of_others", lambda: [])
    monkeypatch.setattr(probes, "SERVICE_ACCOUNT_DIR", tmp_path / "no-sa")
    env = BurstEnv(task_id="t", burst_id="b", repo_dir=tmp_path / "repo", session_dir=tmp_path / "session")
    return env, reachable


def _result(env):
    return json.loads(env.result_path.read_text())


def test_walls_that_hold_are_violations_and_not_breaches(box, monkeypatch):
    env, _ = box
    monkeypatch.setattr(probes.os, "environ", {"CLAUDE_CONFIG_DIR": "/x"})
    assert probes.walls(env) == 0
    result = _result(env)
    assert (result["breaches"], result["controls_held"], result["ok"]) == (0, True, True)
    assert result["attempted"] == 6  # a dev burst; any other role also tries for a GitHub token
    feed = [json.loads(line) for line in env.violations_path.read_text().splitlines()]
    assert len(feed) == result["attempted"]  # every attack that failed is a wall that held
    assert {entry["tool"] for entry in feed} >= {"probe:egress-undeclared-host", "probe:egress-cluster-api"}
    assert all(entry["reason"].endswith("held") for entry in feed)


def test_an_undeclared_host_that_answers_is_a_breach(box, monkeypatch):
    env, reachable = box
    reachable.add(probes.UNDECLARED_HOST)
    monkeypatch.setattr(probes.os, "environ", {})
    assert probes.walls(env) == 3
    result = _result(env)
    assert result["breaches"] == 1 and result["ok"] is False
    (breach,) = [p for p in result["walls"] if p["breached"]]
    assert breach["name"] == "egress-undeclared-host"


def test_a_declared_host_that_does_not_answer_is_a_misplaced_wall_not_a_breach(box, monkeypatch):
    """"If it's required, don't wall it": a box that can reach nothing proves no
    allowlist, so a failed control fails the run without counting as a breach."""
    env, reachable = box
    reachable.clear()
    monkeypatch.setattr(probes.os, "environ", {})
    assert probes.walls(env) == 3
    result = _result(env)
    assert (result["breaches"], result["controls_held"]) == (0, False)


@pytest.mark.parametrize(
    ("environ", "role", "probe"),
    [
        ({"AWS_SECRET_ACCESS_KEY": "s"}, "dev", "store-credential-in-env"),
        ({"X": "https://s3/k?X-Amz-Signature=abc"}, "dev", "store-credential-in-env"),
        ({"GH_TOKEN": "ghs"}, "reviewer", "github-credential-outside-dev"),
    ],
)
def test_a_credential_in_the_box_is_a_breach(box, environ, role, probe):
    env, _ = box
    env.role = role
    found = {p.name: p for p in probes.run_probes(env, environ)}
    assert found[probe].breached


def test_a_dev_burst_is_allowed_its_github_token(box):
    env, _ = box
    names = {p.name for p in probes.run_probes(env, {"GH_TOKEN": "ghs"})}
    assert "github-credential-outside-dev" not in names


def test_a_sibling_containers_presigned_url_is_a_breach(box, monkeypatch):
    env, _ = box
    monkeypatch.setattr(probes, "_environ_of_others", lambda: ["SESSION_GET_URL=https://s?X-Amz-Signature=x"])
    found = {p.name: p for p in probes.run_probes(env, {})}
    assert found["sibling-presigned-url"].breached


def test_a_mounted_service_account_token_is_a_breach(box, tmp_path, monkeypatch):
    env, _ = box
    sa = tmp_path / "sa"
    sa.mkdir()
    (sa / "token").write_text("eyJ", encoding="utf-8")
    monkeypatch.setattr(probes, "SERVICE_ACCOUNT_DIR", sa)
    found = {p.name: p for p in probes.run_probes(env, {})}
    assert found["k8s-service-account-token"].breached
