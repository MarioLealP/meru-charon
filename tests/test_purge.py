"""The fakes answer from responses captured off the live systems (tests/fixtures): namespaces.json from the cluster,
volumes.json from the cluster (two Released PVs on tanuki-build's one folder, a Bound one on charon-build's),
tanuki.yaml from Meru/requests, contents-apps.json is Gitea's directory listing, repo-tanuki.json its repo answer,
zot-tags.json and DIGESTS from Zot."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from charon.cluster import REMOVED_AT, Cluster
from charon.purge import Outside, Switches, purge
from charon.registry import Registry
from charon.repos import AppRepos
from charon.request_files import RequestFiles
from charon.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures"
REQUEST = (FIXTURES / "tanuki.yaml").read_text()
AFTER_RETENTION = datetime(2026, 12, 3, 22, 15, 46, tzinfo=UTC)
DURING_RETENTION = datetime(2026, 12, 3, 22, 15, 45, tzinfo=UTC)


DIGESTS = {
    "4d215b50deaa": "sha256:fcc8a6c072b472e725f17dc3809e7d990ee5c966b135235a11d83bc0f6a17b06",
    "sha256-fcc8a6c072b472e725f17dc3809e7d990ee5c966b135235a11d83bc0f6a17b06.sig": (
        "sha256:6e674079d4d26d72b01dd40d4a26caedd25e24d64f0c425ce17049826396e44c"
    ),
}


class World:
    """Gitea and the cluster as one pass sees them."""

    def __init__(self) -> None:
        self.request = REQUEST
        self.namespaces = json.loads((FIXTURES / "namespaces.json").read_text())
        self.volumes = json.loads((FIXTURES / "volumes.json").read_text())
        self.applications = {"app-thoth"}
        self.deleted: list[tuple[str, dict]] = []
        self.released: list[tuple[str, dict]] = []
        self.secrets_deleted: list[str] = []
        self.login: dict = {}
        self.vault_fails = False
        self.images_deleted: list[str] = []
        self.delete_auth: list[str] = []
        self.repo_gone = False
        self.zot_refuses = False
        self.repo = json.loads((FIXTURES / "repo-tanuki.json").read_text())
        self.repo_status = 200
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

    def openbao(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/auth/kubernetes/login":
            if self.vault_fails:
                return httpx2.Response(403, json={"errors": ["permission denied"]})
            self.login = json.loads(request.content)
            return httpx2.Response(200, json={"auth": {"client_token": "bao-token", "lease_duration": 600}})
        assert request.method == "DELETE"
        assert request.headers["X-Vault-Token"] == "bao-token"
        self.secrets_deleted.append(request.url.path)
        return httpx2.Response(204)

    def zot(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "DELETE":
            self.delete_auth.append(request.headers.get("Authorization", ""))
            if self.zot_refuses:
                return httpx2.Response(403)
            self.images_deleted.append(path)
            return httpx2.Response(202)
        assert "Authorization" not in request.headers
        if self.repo_gone:
            return httpx2.Response(404)
        if path.endswith("/tags/list"):
            return httpx2.Response(200, content=(FIXTURES / "zot-tags.json").read_bytes())
        assert request.method == "HEAD"
        return httpx2.Response(200, headers={"Docker-Content-Digest": DIGESTS[path.rsplit("/", 1)[1]]})

    def anonymous_gitea(self, request: httpx2.Request) -> httpx2.Response:
        assert "Authorization" not in request.headers
        assert request.url.path == "/api/v1/repos/Meru/tanuki"
        return httpx2.Response(self.repo_status, json=self.repo)

    def namespace_gone(self) -> None:
        self.namespaces["items"] = [i for i in self.namespaces["items"] if i["metadata"]["name"] != "tanuki"]

    @property
    def tanuki_volumes(self) -> list[dict]:
        return [v for v in self.volumes["items"] if v["spec"]["claimRef"]["namespace"] == "tanuki-build"]

    def run(
        self, delete: bool, now: datetime, volumes: bool = False, secrets: bool = False, images: bool = False
    ) -> int:
        gitea = httpx2.Client(base_url="https://gitea.test", transport=httpx2.MockTransport(self.gitea))
        cluster = httpx2.Client(base_url="https://cluster.test", transport=httpx2.MockTransport(self.cluster))
        openbao = httpx2.Client(base_url="https://openbao.test", transport=httpx2.MockTransport(self.openbao))
        zot = httpx2.Client(base_url="http://zot.test", transport=httpx2.MockTransport(self.zot))
        switches = Switches(namespaces=delete, volumes=volumes, secrets=secrets, images=images)
        outside = Outside(
            cluster=Cluster(cluster),
            vault=Vault(openbao, "meru-charon-purge", "pod-token"),
            registry=Registry(zot, "charon", "pw"),
            repos=AppRepos(
                httpx2.Client(base_url="https://gitea.test", transport=httpx2.MockTransport(self.anonymous_gitea)),
                "Meru",
            ),
        )
        return purge(RequestFiles(gitea, "Meru/requests"), outside, switches, now)


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


def test_a_namespace_outside_meru_is_never_touched(world: World):
    world.namespace_gone()
    assert world.run(delete=True, now=AFTER_RETENTION) == 0
    assert world.deleted == []


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


def test_the_three_entries_go_once_both_namespaces_are_gone(world: World):
    world.namespace_gone()
    assert world.run(delete=False, now=AFTER_RETENTION, secrets=True) == 0
    assert world.login == {"role": "meru-charon-purge", "jwt": "pod-token"}
    assert world.secrets_deleted == [
        "/v1/apps/metadata/meru/tanuki/eso/env",
        "/v1/apps/metadata/meru/tanuki-build/build",
        "/v1/apps/metadata/meru/tanuki-build/eso/sindri-registration",
    ]


def test_the_secret_switch_off_only_says_so(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    assert world.run(delete=True, now=AFTER_RETENTION, volumes=True, secrets=False) == 0
    assert world.secrets_deleted == []
    assert "tanuki secrets: would delete (dry run)" in caplog.text


def test_secrets_wait_for_the_retention(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    world.run(delete=False, now=DURING_RETENTION, secrets=True)
    assert world.secrets_deleted == []
    assert "tanuki secrets: kept: retention runs to" in caplog.text


def test_secrets_stay_while_a_namespace_exists(world: World, caplog: pytest.LogCaptureFixture):
    world.run(delete=False, now=AFTER_RETENTION, secrets=True)
    assert world.secrets_deleted == []
    assert "tanuki secrets: kept: namespace tanuki still exists" in caplog.text


def test_a_living_argo_application_keeps_the_secrets(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    world.applications.add("app-tanuki")
    world.run(delete=False, now=AFTER_RETENTION, secrets=True)
    assert world.secrets_deleted == []
    assert "tanuki secrets: kept: its Argo Application still exists" in caplog.text


def test_an_app_that_was_not_removed_keeps_its_secrets(world: World):
    world.request = REQUEST.replace("  removed: true\n", "")
    world.namespace_gone()
    world.run(delete=False, now=AFTER_RETENTION, secrets=True)
    assert world.secrets_deleted == []


def test_no_login_fails_the_pass_with_the_switch_off(world: World):
    world.vault_fails = True
    with pytest.raises(httpx2.HTTPStatusError):
        world.run(delete=False, now=DURING_RETENTION)


def test_every_manifest_goes_by_digest_with_the_login(world: World):
    world.namespace_gone()
    assert world.run(delete=False, now=AFTER_RETENTION, images=True) == 0
    assert world.images_deleted == [f"/v2/meru/tanuki/manifests/{digest}" for digest in DIGESTS.values()]
    assert all(auth.startswith("Basic ") for auth in world.delete_auth)


def test_the_image_switch_off_only_says_so(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    assert world.run(delete=True, now=AFTER_RETENTION, volumes=True, secrets=True, images=False) == 0
    assert world.images_deleted == []
    assert "tanuki images: would delete 2 (dry run)" in caplog.text


def test_images_wait_for_the_retention(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    world.run(delete=False, now=DURING_RETENTION, images=True)
    assert world.images_deleted == []
    assert "tanuki images: kept 2: retention runs to" in caplog.text


def test_images_stay_while_a_namespace_exists(world: World, caplog: pytest.LogCaptureFixture):
    world.run(delete=False, now=AFTER_RETENTION, images=True)
    assert world.images_deleted == []
    assert "tanuki images: kept 2: namespace tanuki still exists" in caplog.text


def test_a_repo_zot_no_longer_knows_is_done(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    world.repo_gone = True
    assert world.run(delete=False, now=AFTER_RETENTION, images=True) == 0
    assert world.delete_auth == []
    assert "tanuki images: nothing left" in caplog.text


def test_zot_refusing_a_repo_with_its_own_entry_fails_the_pass(world: World):
    world.namespace_gone()
    world.zot_refuses = True
    assert world.run(delete=False, now=AFTER_RETENTION, images=True) == 1
    assert world.images_deleted == []


def test_a_purged_app_reminds_mario_to_archive(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    assert world.run(delete=False, now=AFTER_RETENTION) == 0
    assert "tanuki repo: not archived, needs Mario's click" in caplog.text


def test_no_reminder_during_the_retention(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    world.run(delete=False, now=DURING_RETENTION)
    assert "needs Mario's click" not in caplog.text


def test_no_reminder_while_a_namespace_exists(world: World, caplog: pytest.LogCaptureFixture):
    world.run(delete=False, now=AFTER_RETENTION)
    assert "needs Mario's click" not in caplog.text


def test_an_archived_repo_needs_no_reminder(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    world.repo["archived"] = True
    world.run(delete=False, now=AFTER_RETENTION)
    assert "needs Mario's click" not in caplog.text


def test_a_repo_that_is_gone_needs_no_reminder(world: World, caplog: pytest.LogCaptureFixture):
    world.namespace_gone()
    world.repo_status = 404
    assert world.run(delete=False, now=AFTER_RETENTION) == 0
    assert "needs Mario's click" not in caplog.text
