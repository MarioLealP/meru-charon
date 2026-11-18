"""AppRepos: anonymous first, the bot's login on an anonymous 404 (private app repos the bot was granted Read on)."""

import httpx2
import pytest

from charon.clients.repos import AppRepos

VALUES = "externalSecrets:\n  env-20261104-aaaaaa: {}\n"


def client(handler) -> httpx2.Client:
    return httpx2.Client(base_url="https://gitea.test", transport=httpx2.MockTransport(handler))


def repos(anonymous, login=None) -> AppRepos:
    return AppRepos(http=client(anonymous), org="Meru", login=client(login) if login else None)


def public_only(request: httpx2.Request) -> httpx2.Response:
    """A private repo as an anonymous reader sees it: nothing."""
    return httpx2.Response(404)


def bot_sees_private(request: httpx2.Request) -> httpx2.Response:
    """The bot has Read on the private repo: its login gets the answers."""
    assert request.headers.get("authorization") == "token bot"
    if request.url.path.endswith("/raw/deploy/values.yaml"):
        return httpx2.Response(200, text=VALUES)
    return httpx2.Response(200, json={"archived": False})


def login_client(inner) -> httpx2.Client:
    return httpx2.Client(
        base_url="https://gitea.test", headers={"Authorization": "token bot"}, transport=httpx2.MockTransport(inner)
    )


def test_a_private_repo_is_read_with_the_bots_login_after_an_anonymous_404():
    r = AppRepos(http=client(public_only), org="Meru", login=login_client(bot_sees_private))
    assert r.wired_entries("nova-lemuria") == {"env-20261104-aaaaaa"}
    assert r.not_archived("nova-lemuria") is True


def test_a_public_repo_never_uses_the_login():
    def anonymous(request: httpx2.Request) -> httpx2.Response:
        assert "authorization" not in request.headers
        if request.url.path.endswith("/raw/deploy/values.yaml"):
            return httpx2.Response(200, text=VALUES)
        return httpx2.Response(200, json={"archived": True})

    def never(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("the login must not be used for a repo the anonymous read can see")

    r = AppRepos(http=client(anonymous), org="Meru", login=login_client(never))
    assert r.wired_entries("echo") == {"env-20261104-aaaaaa"}
    assert r.not_archived("echo") is False


def test_a_repo_neither_can_see_still_raises_so_the_sweep_keeps_its_entries():
    r = AppRepos(http=client(public_only), org="Meru", login=login_client(public_only))
    with pytest.raises(httpx2.HTTPStatusError):
        r.wired_entries("ghost")
    assert r.not_archived("ghost") is False  # a repo that is gone: not open


def test_without_a_login_nothing_changes():
    r = repos(public_only)
    with pytest.raises(httpx2.HTTPStatusError):
        r.wired_entries("nova-lemuria")


def test_a_missing_values_file_in_a_visible_private_repo_means_nothing_wired():
    def bot(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/raw/deploy/values.yaml"):
            return httpx2.Response(404)
        return httpx2.Response(200, json={"archived": False})

    r = AppRepos(http=client(public_only), org="Meru", login=login_client(bot))
    assert r.wired_entries("nova-lemuria") == set()
