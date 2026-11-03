"""The fakes answer from responses captured off the live systems (tests/fixtures): namespaces.json from the cluster,
tanuki.yaml from Meru/requests, contents-apps.json is Gitea's directory listing."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from charon.cluster import REMOVED_AT, Cluster
from charon.purge import purge
from charon.request_files import RequestFiles

FIXTURES = Path(__file__).parent / "fixtures"
REQUEST = (FIXTURES / "tanuki.yaml").read_text()
AFTER_RETENTION = datetime(2026, 12, 3, 22, 15, 46, tzinfo=UTC)
DURING_RETENTION = datetime(2026, 12, 3, 22, 15, 45, tzinfo=UTC)


class World:
    """Gitea and the cluster as one pass sees them."""

    def __init__(self) -> None:
        self.request = REQUEST
        self.namespaces = json.loads((FIXTURES / "namespaces.json").read_text())
        self.applications = {"app-thoth"}
        self.deleted: list[tuple[str, dict]] = []
        self.gitea_fails = False

    @property
    def tanuki(self) -> dict:
        return next(i for i in self.namespaces["items"] if i["metadata"]["name"] == "tanuki")

    def gitea(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/contents/apps"):
            return httpx2.Response(200, content=(FIXTURES / "contents-apps.json").read_bytes())
        if self.gitea_fails:
            return httpx2.Response(500)
        return httpx2.Response(200, text=self.request)

    def cluster(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "DELETE":
            self.deleted.append((path.rsplit("/", 1)[1], json.loads(request.content)))
            return httpx2.Response(200, json={})
        if path == "/api/v1/namespaces":
            assert request.url.params["labelSelector"] == "app.kubernetes.io/part-of=meru"
            return httpx2.Response(200, json=self.namespaces)
        return httpx2.Response(200 if path.rsplit("/", 1)[1] in self.applications else 404, json={})

    def run(self, delete: bool, now: datetime) -> int:
        gitea = httpx2.Client(base_url="https://gitea.test", transport=httpx2.MockTransport(self.gitea))
        cluster = httpx2.Client(base_url="https://cluster.test", transport=httpx2.MockTransport(self.cluster))
        return purge(RequestFiles(gitea, "Meru/requests"), Cluster(cluster), delete, now)


@pytest.fixture
def world(caplog: pytest.LogCaptureFixture) -> World:
    caplog.set_level(logging.INFO, logger="charon")
    return World()


def test_every_check_passed_deletes_that_namespace_by_uid(world: World):
    assert world.run(delete=True, now=AFTER_RETENTION) == 0
    assert world.deleted == [("tanuki", {"preconditions": {"uid": world.tanuki["metadata"]["uid"]}})]


def test_with_the_switch_off_it_only_says_so(world: World, caplog: pytest.LogCaptureFixture):
    assert world.run(delete=False, now=AFTER_RETENTION) == 0
    assert world.deleted == []
    assert "tanuki: would delete (dry run)" in caplog.text


def test_a_second_before_the_retention_ends_it_stays(world: World, caplog: pytest.LogCaptureFixture):
    assert world.run(delete=True, now=DURING_RETENTION) == 0
    assert world.deleted == []
    assert "tanuki: kept: retention runs to 2026-12-03T22:15:46Z" in caplog.text


def test_a_longer_retention_on_the_request_counts(world: World, caplog: pytest.LogCaptureFixture):
    world.request = REQUEST.replace("  removed: true", "  removed: true\n  retention: 60d")
    assert world.run(delete=True, now=AFTER_RETENTION) == 0
    assert world.deleted == []
    assert "retention runs to 2027-01-02T22:15:46Z" in caplog.text


def test_a_namespace_without_the_mark_stays(world: World, caplog: pytest.LogCaptureFixture):
    del world.tanuki["metadata"]["annotations"][REMOVED_AT]
    assert world.run(delete=True, now=AFTER_RETENTION) == 0
    assert world.deleted == []
    assert "no removed-at mark" in caplog.text


def test_a_mark_from_an_earlier_retire_stays(world: World, caplog: pytest.LogCaptureFixture):
    world.tanuki["metadata"]["annotations"][REMOVED_AT] = "2026-10-01T00:00:00Z"
    assert world.run(delete=True, now=AFTER_RETENTION) == 0
    assert world.deleted == []
    assert "isn't the request's" in caplog.text


def test_a_living_argo_application_keeps_it(world: World, caplog: pytest.LogCaptureFixture):
    world.applications.add("app-tanuki")
    assert world.run(delete=True, now=AFTER_RETENTION) == 0
    assert world.deleted == []
    assert "its Argo Application still exists" in caplog.text


def test_a_dry_run_names_everything_in_the_way(world: World, caplog: pytest.LogCaptureFixture):
    world.applications.add("app-tanuki")
    del world.tanuki["metadata"]["annotations"][REMOVED_AT]
    world.run(delete=False, now=DURING_RETENTION)
    assert "retention runs to" in caplog.text
    assert "no removed-at mark" in caplog.text
    assert "its Argo Application still exists" in caplog.text


def test_an_app_that_was_not_removed_is_never_looked_at(world: World):
    world.request = REQUEST.replace("  removed: true\n", "")
    assert world.run(delete=True, now=AFTER_RETENTION) == 0
    assert world.deleted == []


def test_a_removal_without_a_date_stays(world: World, caplog: pytest.LogCaptureFixture):
    world.request = "\n".join(line for line in REQUEST.splitlines() if "removedAt" not in line)
    assert world.run(delete=True, now=AFTER_RETENTION) == 0
    assert world.deleted == []
    assert "no removedAt" in caplog.text


def test_the_build_namespace_goes_with_the_same_checks(world: World):
    build = json.loads(json.dumps(world.tanuki))
    build["metadata"] |= {"name": "tanuki-build", "uid": "build-uid"}
    world.namespaces["items"].append(build)
    world.run(delete=True, now=AFTER_RETENTION)
    assert [name for name, _ in world.deleted] == ["tanuki", "tanuki-build"]


def test_a_namespace_outside_meru_is_never_touched(world: World, caplog: pytest.LogCaptureFixture):
    world.namespaces["items"] = [i for i in world.namespaces["items"] if i["metadata"]["name"] != "tanuki"]
    assert world.run(delete=True, now=AFTER_RETENTION) == 0
    assert world.deleted == []
    assert "tanuki: no namespace left" in caplog.text


def test_a_failed_read_fails_the_pass_and_deletes_nothing(world: World):
    world.gitea_fails = True
    assert world.run(delete=True, now=AFTER_RETENTION) == 1
    assert world.deleted == []


def test_a_broken_file_fails_the_pass_too(world: World):
    world.request = "metadata: ["
    assert world.run(delete=True, now=AFTER_RETENTION) == 1
