"""Platform-owned clones (issue #4): sync semantics + credential hygiene."""

import subprocess

import pytest
from control_plane.clone import CloneError, _git, prepare_clone


def _origin(tmp_path):
    """A file:// origin repo with one commit on main."""
    origin = tmp_path / "origin"
    origin.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=origin, check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.name", "Origin Owner")
    run("config", "user.email", "owner@example.com")
    (origin / "README.md").write_text("v1\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "chore: init")
    return origin


def test_first_prepare_clones_then_resyncs_burst_damage(tmp_path):
    origin = _origin(tmp_path)
    clones = tmp_path / "clones"

    clone = prepare_clone(clones, "owner/product", url=origin.as_uri())
    assert (clone / "README.md").read_text() == "v1\n"
    assert clone == clones / "owner__product"

    # simulate what a burst leaves behind: branch checked out, bot identity
    # in local config, dirty + untracked files
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=clone, check=True, capture_output=True
    )
    run("config", "user.name", "bot[bot]")
    run("checkout", "-qb", "dev/9-something")
    (clone / "README.md").write_text("mutated", encoding="utf-8")
    (clone / "stray.txt").write_text("junk", encoding="utf-8")

    # origin moves on; the next prepare must match it exactly
    (origin / "README.md").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-aqm", "feat: v2"], cwd=origin, check=True, capture_output=True)

    clone2 = prepare_clone(clones, "owner/product", url=origin.as_uri())
    assert clone2 == clone
    assert (clone / "README.md").read_text() == "v2\n"
    assert not (clone / "stray.txt").exists()
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=clone, capture_output=True, text=True
    ).stdout.strip()
    assert branch == "main"


def test_git_errors_never_echo_the_auth_header(tmp_path):
    with pytest.raises(CloneError) as err:
        _git(
            [
                "-c",
                "http.https://github.com/.extraheader=Authorization: Basic SECRET-B64",
                "clone",
                "file:///nonexistent-origin",
                str(tmp_path / "x"),
            ]
        )
    assert "SECRET-B64" not in str(err.value)
    assert "git clone failed" in str(err.value)


def test_prepare_clone_ref_checks_out_pr_head_detached(tmp_path):
    """Assess bursts review the PR head, not origin's default branch."""
    origin = _origin(tmp_path)
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=origin, check=True, capture_output=True, text=True
    )
    run("checkout", "-qb", "dev/5-feature")
    (origin / "feature.txt").write_text("new\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "feat: the change under review")
    head_sha = run("rev-parse", "HEAD").stdout.strip()
    run("checkout", "-q", "main")

    clones = tmp_path / "clones"
    clone = prepare_clone(clones, "owner/product", url=origin.as_uri(), ref=head_sha)
    assert (clone / "feature.txt").read_text() == "new\n"
    at = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True
    ).stdout.strip()
    assert at == head_sha

    # and the next default sync returns it to main
    clone2 = prepare_clone(clones, "owner/product", url=origin.as_uri())
    assert not (clone2 / "feature.txt").exists()
