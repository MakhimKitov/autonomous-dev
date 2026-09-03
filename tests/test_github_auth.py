"""GitHub App auth: JWT shape, installation discovery, token minting — offline."""

import json

import jwt as pyjwt
import pytest
from control_plane.github_auth import (
    ROLE_PREFIXES,
    AppCredentials,
    GitHubAuthError,
    bot_git_identity,
    discover_installation,
    make_app_jwt,
    mint_installation_token,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_KEY = _KEY.public_key()

CREDS = AppCredentials(app_id="12345", private_key=PRIVATE_PEM)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_jwt_is_rs256_with_app_issuer():
    token = make_app_jwt(CREDS, now=1_000_000)
    claims = pyjwt.decode(token, PUBLIC_KEY, algorithms=["RS256"], options={"verify_exp": False})
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "12345"}


def test_credentials_from_env(tmp_path):
    assert AppCredentials.from_env({}) is None
    key_file = tmp_path / "app.pem"
    key_file.write_text(PRIVATE_PEM, encoding="utf-8")
    creds = AppCredentials.from_env(
        {"GITHUB_APP_ID": "7", "GITHUB_APP_PRIVATE_KEY_PATH": str(key_file)}
    )
    assert creds and creds.app_id == "7" and "PRIVATE KEY" in creds.private_key
    with pytest.raises(GitHubAuthError, match="both"):
        AppCredentials.from_env({"GITHUB_APP_ID": "7"})
    with pytest.raises(GitHubAuthError, match="does not exist"):
        AppCredentials.from_env(
            {"GITHUB_APP_ID": "7", "GITHUB_APP_PRIVATE_KEY_PATH": "/nope.pem"}
        )


def test_the_role_prefix_scheme_is_the_contract():
    # TO-BE §4 (D1): App names are the adopter's choice — the platform knows
    # roles only through these prefixes. The dev App reads the bare names.
    assert ROLE_PREFIXES == {
        "dev": "",
        "reviewer": "REVIEWER_",
        "tester": "TESTER_",
        "monitor": "MONITOR_",
    }


def test_credentials_from_env_behind_a_role_prefix(tmp_path):
    key_file = tmp_path / "tester.pem"
    key_file.write_text(PRIVATE_PEM, encoding="utf-8")
    env = {
        "GITHUB_APP_ID": "1",  # the dev App must never bleed into another role
        "TESTER_GITHUB_APP_ID": "9",
        "TESTER_GITHUB_APP_PRIVATE_KEY_PATH": str(key_file),
        "TESTER_GITHUB_APP_INSTALLATION_ID": "77",
    }
    creds = AppCredentials.from_env(env, prefix="TESTER_")
    assert creds and creds.app_id == "9" and creds.installation_id == "77"
    assert AppCredentials.from_env(env, prefix="MONITOR_") is None
    with pytest.raises(GitHubAuthError, match="TESTER_GITHUB_APP_ID"):
        AppCredentials.from_env({"TESTER_GITHUB_APP_PRIVATE_KEY_PATH": str(key_file)}, prefix="TESTER_")


def test_discovery_single_installation():
    def get(url, **kwargs):
        assert url.endswith("/app/installations")
        return FakeResponse([{"id": 42, "app_slug": "auto-dev"}])

    assert discover_installation(CREDS, http_get=get) == ("42", "auto-dev")


def test_discovery_requires_pin_when_multiple():
    def get(url, **kwargs):
        return FakeResponse([{"id": 1, "app_slug": "a"}, {"id": 2, "app_slug": "a"}])

    with pytest.raises(GitHubAuthError, match="multiple installations"):
        discover_installation(CREDS, http_get=get)


def test_mint_token_and_identity():
    def get(url, **kwargs):
        return FakeResponse([{"id": 42, "app_slug": "auto-dev"}])

    def post(url, **kwargs):
        assert url.endswith("/app/installations/42/access_tokens")
        return FakeResponse(
            {
                "token": "ghs_secret",
                "expires_at": "2026-07-02T13:00:00Z",
                "permissions": {"contents": "write", "pull_requests": "write"},
            },
            status_code=201,
        )

    minted = mint_installation_token(CREDS, http_get=get, http_post=post)
    assert minted.token == "ghs_secret"
    assert minted.bot_login == "auto-dev[bot]"
    name, email = bot_git_identity(minted, CREDS.app_id)
    assert name == "auto-dev[bot]"
    assert email == "12345+auto-dev[bot]@users.noreply.github.com"


def test_http_errors_surface():
    def get(url, **kwargs):
        return FakeResponse({"message": "bad credentials"}, status_code=401)

    with pytest.raises(GitHubAuthError, match="401"):
        discover_installation(CREDS, http_get=get)
