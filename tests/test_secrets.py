"""SecretResolver (TO-BE §4, D4): refs are names; values are read at use, from
env, a file, or the one out-of-band helper — never from the ledger."""

import stat

import pytest
from secret_refs import (
    SCHEMES,
    SECRET_HELPER_ENV,
    SecretError,
    SecretResolver,
    parse_ref,
)


def _helper(tmp_path, body: str):
    """A tiny helper binary: ``<helper> get NAME`` — the git-credential shape."""
    script = tmp_path / "secret-helper"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def test_the_three_schemes_are_the_grammar():
    assert SCHEMES == ("env", "file", "helper")
    assert parse_ref("env:TEST_TOKEN") == ("env", "TEST_TOKEN")
    assert parse_ref("file:/run/secrets/token") == ("file", "/run/secrets/token")
    assert parse_ref("helper:discord/test-token") == ("helper", "discord/test-token")


def test_a_value_never_passes_for_a_ref():
    # no scheme, an unknown scheme, or a scheme naming nothing — all refused;
    # `vault:` is exactly what the platform must not grow a special case for
    for bad in ("ghp_abc123", "vault:secret/x", "env:", "", "s3://bucket/key"):
        with pytest.raises(SecretError, match="never a value|names nothing"):
            parse_ref(bad)


def test_a_bare_path_keeps_its_legacy_meaning_only_where_asked():
    # *_GITHUB_APP_PRIVATE_KEY_PATH read a plain path before D4 — still does
    assert parse_ref("/keys/app.pem", bare_as="file") == ("file", "/keys/app.pem")
    assert parse_ref("~/keys/app.pem", bare_as="file") == ("file", "~/keys/app.pem")
    with pytest.raises(SecretError):
        parse_ref("/keys/app.pem")  # a --test-env ref must say file:


def test_env_refs_read_the_given_environ():
    resolver = SecretResolver({"TEST_TOKEN": "tok\n", "EMPTY": "  "})
    assert resolver.resolve("env:TEST_TOKEN") == "tok"  # trailing newline dropped
    assert resolver.probe("env:TEST_TOKEN") is True
    for unset in ("env:EMPTY", "env:NOPE"):
        assert resolver.probe(unset) is False
        with pytest.raises(SecretError, match="is unset"):
            resolver.resolve(unset)


def test_environ_none_reads_the_live_process_env(monkeypatch):
    # the scheduler's resolver: a value provisioned after construction is seen
    resolver = SecretResolver()
    monkeypatch.delenv("D4_LIVE_PROBE", raising=False)
    assert resolver.probe("env:D4_LIVE_PROBE") is False
    monkeypatch.setenv("D4_LIVE_PROBE", "now")
    assert resolver.resolve("env:D4_LIVE_PROBE") == "now"


def test_file_refs_read_the_file_without_its_trailing_newline(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("from-file\n", encoding="utf-8")
    resolver = SecretResolver({})
    assert resolver.probe(f"file:{secret}") is True
    assert resolver.resolve(f"file:{secret}") == "from-file"
    assert resolver.probe(f"file:{tmp_path / 'missing'}") is False
    with pytest.raises(SecretError, match="does not exist"):
        resolver.resolve(f"file:{tmp_path / 'missing'}")


def test_helper_refs_ask_the_configured_binary(tmp_path):
    helper = _helper(
        tmp_path,
        'test "$1" = get || exit 3\n'
        'case "$2" in discord/test-token) echo "from-helper";; *) echo "unknown $2" >&2; exit 1;; esac\n',
    )
    resolver = SecretResolver({SECRET_HELPER_ENV: helper})
    assert resolver.resolve("helper:discord/test-token") == "from-helper"
    assert resolver.probe("helper:anything") is True  # configured + executable, not invoked
    with pytest.raises(SecretError, match="exit 1.*unknown other"):
        resolver.resolve("helper:other")


def test_helper_scheme_is_unavailable_until_configured(tmp_path):
    resolver = SecretResolver({})
    assert resolver.probe("helper:x") is False
    with pytest.raises(SecretError, match=SECRET_HELPER_ENV):
        resolver.resolve("helper:x")
    broken = SecretResolver({SECRET_HELPER_ENV: str(tmp_path / "not-there")})
    assert broken.probe("helper:x") is False
    with pytest.raises(SecretError, match="not an executable"):
        broken.resolve("helper:x")


def test_helper_answering_nothing_is_an_error(tmp_path):
    helper = _helper(tmp_path, "exit 0\n")
    with pytest.raises(SecretError, match="returned nothing"):
        SecretResolver({SECRET_HELPER_ENV: helper}).resolve("helper:x")

