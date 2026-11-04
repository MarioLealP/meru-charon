"""The fakes answer from responses captured off the live systems (tests/fixtures): namespaces.json from the cluster,
volumes.json from the cluster (two Released PVs on tanuki-build's one folder, a Bound one on charon-build's),
tanuki.yaml from Meru/requests, contents-apps.json is Gitea's directory listing."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from charon.cluster import REMOVED_AT, Cluster
from charon.purge import Switches, purge
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
        self.volumes = json.loads((FIXTURES / "volumes.json").read_text())
        self.applications = {"app-thoth"}
        self.deleted: list[tuple[str, dict]] = []
        self.released: list[tuple[str, dict]] = []
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
        if request.method == "PATCH":
            assert request.headers["Content-Type"] == "application/merge-patch+json"
            self.released.append((path.rsplit("/", 1)[1], json.loads(request.content)))
            return httpx2.Response(200, json={})
        if path == "/api/v1/persistentvolumes":
            return httpx2.Response(200, json=self.volumes)
        if path == "/api/v1/namespaces":
            assert request.url.params["labelSelector"] == "app.kubernetes.io/part-of=meru"
            return httpx2.Response(200, json=self.namespaces)
        return httpx2.Response(200 if path.rsplit("/", 1)[1] in self.applications else 404, json={})

    def namespace_gone(self) -> None:
        self.namespaces["items"] = [i for i in self.namespaces["items"] if i["metadata"]["name"] != "tanuki"]

    @property
    def tanuki_volumes(self) -> list[dict]:
        return [v for v in self.volumes["items"] if v["spec"]["claimRef"]["namespace"] == "tanuki-build"]

    def run(self, delete: bool, now: datetime, volumes: bool = False) -> int:
        gitea = httpx2.Client(base_url="https://gitea.test", transport=httpx2.MockTransport(self.gitea))
        cluster = httpx2.Client(base_url="https://cluster.test", transport=httpx2.MockTransport(self.cluster))
        switches = Switches(namespaces=delete, volumes=volumes)
        return purge(RequestFiles(gitea, "Meru/requests"), Cluster(cluster), switches, now)


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
    world.namespace_gone()
    world.volumes["items"] = []
    assert world.run(delete=True, now=AFTER_RETENTION) == 0
    assert world.deleted == []
    assert "tanuki: nothing left" in caplog.text


def test_a_failed_read_fails_the_pass_and_deletes_nothing(world: World):
    world.gitea_fails = True
    assert world.run(delete=True, now=AFTER_RETENTION) == 1
    assert world.deleted == []


def test_a_broken_file_fails_the_pass_too(world: World):
    world.request = "metadata: ["
    assert world.run(delete=True, now=AFTER_RETENTION) == 1


def test_a_volume_goes_once_its_namespace_is_gone_each_by_uid(world: World):
    world.namespace_gone()
    assert world.run(delete=False, now=AFTER_RETENTION, volumes=True) == 0
    assert world.released == [
        (
            v["metadata"]["name"],
            {"metadata": {"uid": v["metadata"]["uid"]}, "spec": {"persistentVolumeReclaimPolicy": "Delete"}},
        )
        for v in world.tanuki_volumes
    ]


def test_the_volume_switch_off_only_says_so(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    assert world.run(delete=True, now=AFTER_RETENTION, volumes=False) == 0
    assert world.released == []
    assert "would release (dry run)" in caplog.text


def test_the_namespace_switch_does_not_release_volumes(world: World):
    assert world.run(delete=True, now=AFTER_RETENTION, volumes=False) == 0
    assert [name for name, _ in world.deleted] == ["tanuki"]
    assert world.released == []


def test_a_volume_waits_for_the_retention(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    world.run(delete=False, now=DURING_RETENTION, volumes=True)
    assert world.released == []
    assert "tanuki-build volume" in caplog.text
    assert "kept: retention runs to" in caplog.text


def test_a_volume_stays_while_its_namespace_exists(world: World, caplog: pytest.LogCaptureFixture):
    build = json.loads(json.dumps(world.tanuki))
    build["metadata"] |= {"name": "tanuki-build", "uid": "build-uid"}
    world.namespaces["items"].append(build)
    world.run(delete=True, now=AFTER_RETENTION, volumes=True)
    assert world.released == []
    assert "namespace tanuki-build still exists" in caplog.text


def test_a_living_argo_application_keeps_the_volumes(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    world.applications.add("app-tanuki")
    world.run(delete=False, now=AFTER_RETENTION, volumes=True)
    assert world.released == []
    assert "its Argo Application still exists" in caplog.text


def test_a_volume_in_use_on_the_same_folder_keeps_the_released_ones(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    world.tanuki_volumes[0]["status"]["phase"] = "Bound"
    world.run(delete=False, now=AFTER_RETENTION, volumes=True)
    assert world.released == []
    assert "a volume in use shares /home/k8s/volumes/tanuki-build/sindri-data" in caplog.text


def test_a_volume_already_set_to_delete_is_left_alone(world: World):
    world.namespace_gone()
    world.tanuki_volumes[0]["spec"]["persistentVolumeReclaimPolicy"] = "Delete"
    world.run(delete=False, now=AFTER_RETENTION, volumes=True)
    assert [name for name, _ in world.released] == [world.tanuki_volumes[1]["metadata"]["name"]]


def test_another_apps_volumes_are_never_touched(world: World):
    world.namespace_gone()
    world.run(delete=False, now=AFTER_RETENTION, volumes=True)
    charon = next(v for v in world.volumes["items"] if v["spec"]["claimRef"]["namespace"] == "charon-build")
    assert charon["metadata"]["name"] not in [name for name, _ in world.released]
