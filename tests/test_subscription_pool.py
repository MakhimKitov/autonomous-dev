"""The subscription pool's operator surface, and its one claim (F1, #38).

The claim: **the platform never holds a token**. A member materializes their
OAuth token themselves and registers a *ref* — so there is no column, no log
line and no archive that could carry the value, because nothing in the platform
ever sees one. This file is where that is made a fact rather than a promise:

- the grammar refuses a value where a ref belongs, at every entrance;
- so does the schema beneath it, and neither refusal *echoes* what it refused —
  an operator who pastes a token must not see it land in their terminal or this
  process's log;
- and the done-when: with a token-shaped value pushed at every entrance the
  verbs have, the whole ledger file, every audit row, every event payload
  and everything the process printed or logged is greppable for it and empty.

Through ``build_parser`` + the ``_subscription`` handler, the way
`test_registry_cli.py` drives the registry verbs. The store-level contract —
audit-first mutations, CAS endings, both backends — is `test_ledger_contract.py`'s.
"""

import json
import sqlite3

import pytest
from control_plane import cli
from control_plane.models import SubscriptionStatus
from control_plane.store import SqliteLedger
from control_plane.subscriptions import (
    SubscriptionError,
    check_cap,
    check_expiry,
    check_ref,
    check_settable,
    subscription_id_for,
)

# What a real `claude setup-token` value looks like enough to grep for. Nothing
# in this repo should ever be able to store, print or log a string like it.
TOKEN = "sk-ant-oat01-GREPPABLE-TOKEN-DO-NOT-STORE-9f3c1a"


def run(tmp_path, *argv):
    args = cli.build_parser().parse_args(
        ["subscription", *argv, "--data-dir", str(tmp_path), "--actor", "op"]
    )
    return cli._subscription(args)


def register(tmp_path, member="maksim", ref="env:MAKSIM_CLAUDE_TOKEN", cap="12000000",
             expires="2027-09-01", reason="opting in"):
    return run(tmp_path, "register", "--member", member, "--ref", ref,
               "--cap", cap, "--expires", expires, "--reason", reason)


def stored(tmp_path, subscription_id="maksim"):
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    try:
        return ledger.get_subscription(subscription_id)
    finally:
        ledger.close()


# --- the grammar (the engine side) ---


def test_the_id_is_derived_from_the_member(tmp_path):
    assert subscription_id_for("Maksim") == "maksim"
    assert subscription_id_for(" Ada Lovelace ") == "ada-lovelace"
    assert subscription_id_for("dev.bot_2") == "dev.bot_2"
    for bad in ("", "  ", "-leading", "who?", "a/b"):
        with pytest.raises(SubscriptionError, match="member handle"):
            subscription_id_for(bad)


def test_a_ref_is_a_name_and_the_refusal_says_nothing_more(tmp_path):
    assert check_ref(" env:MAKSIM_CLAUDE_TOKEN ") == "env:MAKSIM_CLAUDE_TOKEN"
    assert check_ref("file:/run/secrets/maksim") == "file:/run/secrets/maksim"
    assert check_ref("helper:maksim") == "helper:maksim"
    for value in (TOKEN, "", "MAKSIM_CLAUDE_TOKEN", "vault:maksim"):
        with pytest.raises(SubscriptionError) as refused:
            check_ref(value)
        assert "refs, never tokens" in str(refused.value)
        # the wall's other half: the refusal never repeats its input, and no
        # chained cause carries it into a traceback either
        assert value not in str(refused.value) or not value
        assert refused.value.__cause__ is None


def test_a_cap_is_capacity_and_an_expiry_is_ahead(tmp_path):
    assert check_cap(10) == 10
    for bad in (0, -1):
        with pytest.raises(SubscriptionError, match="must be positive"):
            check_cap(bad)
    assert check_expiry("2027-09-01") == "2027-09-01T00:00:00+00:00"
    assert check_expiry("2027-09-01T10:00:00+03:00") == "2027-09-01T07:00:00+00:00"
    with pytest.raises(SubscriptionError, match="already past"):
        check_expiry("2020-01-01")
    with pytest.raises(SubscriptionError, match="date or an ISO timestamp"):
        check_expiry("next year")


def test_set_touches_the_declaration_and_nothing_else():
    check_settable({"cap_tokens": 5})
    with pytest.raises(SubscriptionError, match="nothing to set"):
        check_settable({})
    for column in ("status", "backoff_until", "subscription_id", "member", "registered_at"):
        with pytest.raises(SubscriptionError, match="not settable"):
            check_settable({column: "x"})


# --- the verbs ---


def test_a_registration_is_the_declaration_and_its_trail(tmp_path, capsys):
    assert register(tmp_path) == 0
    row = stored(tmp_path)
    assert row.member == "maksim" and row.secret_ref == "env:MAKSIM_CLAUDE_TOKEN"
    assert (row.cap_tokens, row.status, row.backoff_until) == (12_000_000, "active", None)
    assert row.expires_at == "2027-09-01T00:00:00+00:00"

    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    try:
        entry = ledger.audit_trail("maksim")[-1]
        assert (entry.actor, entry.action, entry.reason) == ("op", "subscription.register", "opting in")
        (event,) = [e for e in ledger.events() if e.kind == "subscription_registered"]
        assert event.payload["subscription"] == "maksim" and event.payload["audit_id"] == entry.id
    finally:
        ledger.close()
    assert "env:MAKSIM_CLAUDE_TOKEN" in capsys.readouterr().out  # a ref is printable; a token is not


def test_one_member_one_contribution(tmp_path, capsys):
    register(tmp_path)
    assert register(tmp_path, cap="1") == 2
    assert "already contributes" in capsys.readouterr().err
    assert stored(tmp_path).cap_tokens == 12_000_000  # the first declaration stands


def test_set_changes_the_cap_the_expiry_and_the_ref(tmp_path):
    register(tmp_path)
    assert run(tmp_path, "set", "maksim", "--cap", "20000000", "--expires", "2028-01-01",
               "--ref", "file:/run/secrets/maksim", "--reason", "moved to a file") == 0
    row = stored(tmp_path)
    assert (row.cap_tokens, row.expires_at, row.secret_ref) == (
        20_000_000, "2028-01-01T00:00:00+00:00", "file:/run/secrets/maksim",
    )
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    try:
        (event,) = [e for e in ledger.events() if e.kind == "subscription_updated"]
        assert event.payload["changed"] == ["cap_tokens", "expires_at", "secret_ref"]
    finally:
        ledger.close()


def test_revoke_is_final_and_says_why(tmp_path, capsys):
    register(tmp_path)
    assert run(tmp_path, "revoke", "maksim", "--reason", "left the team") == 0
    assert stored(tmp_path).status == SubscriptionStatus.REVOKED
    assert run(tmp_path, "revoke", "maksim", "--reason", "again") == 2
    assert "already revoked" in capsys.readouterr().err
    assert [a.action for a in _audit(tmp_path)] == ["subscription.register", "subscription.revoke"]


def test_a_verb_against_nobody_is_a_refusal_not_a_row(tmp_path, capsys):
    assert run(tmp_path, "show", "ghost") == 2
    assert run(tmp_path, "revoke", "ghost", "--reason", "x") == 2
    assert run(tmp_path, "set", "ghost", "--cap", "5") == 2
    assert capsys.readouterr().err.count("not a registered subscription") == 3
    assert stored(tmp_path, "ghost") is None


def test_list_and_show_read_without_a_row_to_read(tmp_path, capsys):
    assert run(tmp_path, "list") == 0
    assert "no subscriptions contributed" in capsys.readouterr().out
    register(tmp_path)
    assert run(tmp_path, "list") == 0
    assert "maksim  active" in capsys.readouterr().out


def _audit(tmp_path, target="maksim"):
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    try:
        return ledger.audit_trail(target)
    finally:
        ledger.close()


# --- the wall: a token cannot get in, and cannot get out ---


def test_a_token_where_a_ref_belongs_is_refused_at_every_entrance(tmp_path, capsys):
    assert register(tmp_path, ref=TOKEN) == 2
    assert "refs, never tokens" in capsys.readouterr().err
    assert stored(tmp_path) is None

    register(tmp_path)  # now a real contribution, and `set` guards the same way
    assert run(tmp_path, "set", "maksim", "--ref", TOKEN) == 2
    assert "refs, never tokens" in capsys.readouterr().err
    assert stored(tmp_path).secret_ref == "env:MAKSIM_CLAUDE_TOKEN"


def test_the_schema_refuses_what_reaches_past_the_verbs(tmp_path):
    """The wall behind the wall: a console, a psql session or a bug that binds a
    value straight at the table meets the column's own CHECK — and the error the
    platform raises from it says the rule without repeating the row."""
    register(tmp_path)
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    try:
        # a token, and a scheme in the wrong case — the twin of the Postgres
        # regex is case-sensitive too, or the two backends would differ
        for not_a_ref in (TOKEN, "ENV:MAKSIM_CLAUDE_TOKEN", "env:", "vault:maksim"):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                ledger._conn.execute(
                    "INSERT INTO subscriptions VALUES ('bob','bob',?,1,'2027-01-01','active',NULL,'2026-01-01')",
                    (not_a_ref,),
                )
            ledger._conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            ledger._conn.execute(
                "INSERT INTO subscriptions VALUES ('bob','bob','env:BOB',0,'2027-01-01','active',NULL,'2026-01-01')"
            )
        ledger._conn.rollback()
    finally:
        ledger.close()


def test_no_token_material_anywhere_the_platform_writes(tmp_path, capsys, caplog):
    """F1's done-when, greppable: push a token at every entrance that takes one,
    then read back everything the platform wrote — the ledger file byte for byte,
    every audit row, every event payload, stdout, stderr and the log stream — and
    find nothing of it. It never becomes a row, so it can never become a dump.

    "Every entrance that takes one" is the ref, everywhere a ref is accepted.
    `--member` and `--reason` are prose an operator types about themselves, like
    a commit message; a token pasted there is a row the platform had no way to
    tell from a name, and the answer to that is the same as for any other place
    a person can type a secret — not a sniffer in the verb.
    """
    for argv in (
        ["register", "--member", "maksim", "--ref", TOKEN, "--cap", "10", "--expires", "2027-01-01"],
        ["register", "--member", "maksim", "--ref", "helper:", "--cap", "10", "--expires", "2027-01-01"],
        ["register", "--member", "maksim", "--ref", "env:MAKSIM", "--cap", "10",
         "--expires", "2027-01-01", "--reason", "materialized by hand"],
        ["set", "maksim", "--ref", TOKEN],
        ["set", "maksim", "--cap", "20", "--reason", "bump"],
        ["revoke", "maksim", "--reason", "rotating the token"],
        ["show", "maksim"],
        ["list"],
    ):
        run(tmp_path, *argv)

    printed = capsys.readouterr()
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    try:
        rows = [
            json.dumps(dict(row))
            for table in ("subscriptions", "audit", "events")
            for row in ledger._conn.execute(f"SELECT * FROM {table}")
        ]
    finally:
        ledger.close()
    dumps = {
        "the ledger file": (tmp_path / "ledger.sqlite3").read_bytes().decode("utf-8", "ignore"),
        "the rows": "\n".join(rows),
        "stdout": printed.out,
        "stderr": printed.err,
        "the log": caplog.text,
    }
    for what, blob in dumps.items():
        assert TOKEN not in blob, f"token material in {what}"
        assert "sk-ant" not in blob, f"token material in {what}"
    # and the rows the pool did write are the refs it was given
    assert "env:MAKSIM" in dumps["the rows"]
