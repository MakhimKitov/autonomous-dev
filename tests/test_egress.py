"""A burst's egress, declared as data (G6): what the pod may reach, and why.

The declaration is the platform's half of the network wall — enforcement is the
adopter's CNI — so what is tested is the data: every host a burst needs is in
it (a wall in the way of required work is a bug), nothing else is, and it is on
the Job and on the pod, where a policy engine reads it.
"""

import json

import pytest
from control_plane.egress import EGRESS_ANNOTATION, declaration
from control_plane.testing import ManifestError, parse_manifest
from dev_agent import env as agentenv
from test_jobs_runner import _spec, manifest, runner  # noqa: F401 — the fixture


def _hosts(entries):
    return [(entry.host, entry.port) for entry in entries]


def test_a_dev_burst_declares_github_the_engine_and_its_toolchain(monkeypatch):
    monkeypatch.delenv("DEV_AGENT_OTLP_ENDPOINT", raising=False)
    hosts = _hosts(declaration(_spec()))
    assert hosts == [
        ("github.com", 443), ("api.github.com", 443),
        ("api.anthropic.com", 443), ("pypi.org", 443), ("files.pythonhosted.org", 443),
    ]


def test_a_burst_without_the_engine_declares_no_inference(monkeypatch):
    monkeypatch.delenv("DEV_AGENT_OTLP_ENDPOINT", raising=False)
    hosts = {host for host, _ in _hosts(declaration(_spec(command="walls")))}
    assert hosts == {"github.com", "api.github.com"}


def test_the_archive_and_the_otlp_endpoint_are_declared_by_the_urls_they_are(monkeypatch):
    monkeypatch.setenv("DEV_AGENT_OTLP_ENDPOINT", "http://collector.observability:4318")
    urls = ["http://10.0.0.5:8333/bucket/k?X-Amz-Signature=a", "http://10.0.0.5:8333/bucket/k2?sig"]
    entries = declaration(_spec(), urls)
    assert ("10.0.0.5", 8333) in _hosts(entries)
    assert _hosts(entries).count(("10.0.0.5", 8333)) == 1  # once, however many URLs
    assert ("collector.observability", 4318) in _hosts(entries)
    assert all(entry.why for entry in entries)


def test_only_a_tester_declares_the_products_sandbox():
    sandbox = ("discord.com", "gateway.discord.gg")
    tester = {host for host, _ in _hosts(declaration(_spec(role="tester", egress=sandbox)))}
    dev = {host for host, _ in _hosts(declaration(_spec(role="dev", egress=sandbox)))}
    assert set(sandbox) <= tester
    assert not set(sandbox) & dev


def test_the_declaration_is_on_the_job_and_on_the_pod(runner):  # noqa: F811
    job = manifest(runner, _spec())
    on_job = json.loads(job["metadata"]["annotations"][EGRESS_ANNOTATION])
    on_pod = json.loads(job["spec"]["template"]["metadata"]["annotations"][EGRESS_ANNOTATION])
    assert on_job == on_pod
    assert {"host": "store.example", "port": 443, "why": "the archive: the phases' presigned URLs"} in on_job
    assert job["spec"]["template"]["metadata"]["labels"]["role"] == "dev"  # what a policy selects by
    assert agentenv.CONTAINER_SESSION_DIR  # the pod is otherwise unchanged (test_jobs_runner)


def test_the_manifest_declares_the_sandbox_by_name():
    text = "---\nenv: [DISCORD_TOKEN]\ntools: []\negress: [discord.com, gateway.discord.gg]\n---\nprose\n"
    assert parse_manifest(text).egress == ("discord.com", "gateway.discord.gg")
    assert parse_manifest("---\nenv: []\n---\n").egress == ()


@pytest.mark.parametrize("host", ["*.discord.com", "10.0.0.1", "https://discord.com", "Discord.COM"])
def test_a_manifest_host_is_a_name_not_a_pattern_or_an_address(host):
    with pytest.raises(ManifestError, match="not a host name"):
        parse_manifest(f"---\negress: [{host}]\n---\n")


def test_the_reference_policy_allows_exactly_the_declarations_fixed_hosts():
    """examples/egress/ is the declaration's platform-wide half written for one
    CNI. It must not drift from the code that declares: a host the policy
    misses is a wall in front of required work, one it adds is a hole."""
    import re
    from pathlib import Path

    from control_plane.egress import GITHUB_HOSTS, INFERENCE_HOST, TOOLCHAIN_HOSTS

    policy = (Path(__file__).parent.parent / "examples" / "egress" / "cilium-burst-egress.yaml").read_text()
    allowed = set(re.findall(r"matchName:\s*(\S+)", policy))
    assert allowed == {*GITHUB_HOSTS, INFERENCE_HOST, *TOOLCHAIN_HOSTS}
