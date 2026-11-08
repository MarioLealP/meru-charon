"""The change-entry sweep (D124) against fakes of Gitea, OpenBao and the cluster's ConfigMap API."""

import json
from datetime import UTC, datetime

import httpx2
import pytest
from pydantic import SecretStr

from charon.clients.cluster import Cluster
from charon.clients.registry import Registry
from charon.clients.repos import AppRepos
from charon.clients.request_files import RequestFiles
from charon.clients.vault import Vault
from charon.purge.purge import Outside
from charon.purge.removal import Removals
from charon.sweep.sweep import Report, SweepReport, sweep

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
OLD = "2026-11-01T10:00:00.123456789Z"
FRESH = "2026-11-05T06:00:00Z"  # six hours before NOW: inside the day's grace


def request(entry_a: str, removed: bool = False) -> str:
    return f"""apiVersion: meru.clockmost.com/v1
kind: App
metadata:
  name: kappa
  approvedBy: akadmin
spec:
  owner: mario
  description: Sweep test app
  route:
    enabled: false
  secrets:
    - name: a
      entry: {entry_a}
    - name: b
{"  removed: true" if removed else ""}
"""


VALUES = """image: x
externalSecrets:
  env-20261102-bbbbbb: {}
  env: {}
secretEnv:
  B:
    secret: env-20261102-bbbbbb
    key: b
"""


class World:
    def __init__(self) -> None:
        self.main = request("env-20261101-aaaaaa")
        self.pr = request("env-20261103-cccccc")  # an open change on its way in
        self.pulls = [{"number": 7, "head": {"sha": "abc123"}}]
        self.values: str | None = VALUES
        self.repo_visible = True
        self.entries = {
            "kappa": {
                "env": OLD,
                "env-20261101-aaaaaa": OLD,
                "env-20261102-bbbbbb": OLD,
                "env-20261103-cccccc": OLD,
                "env-20261104-dddddd": OLD,
                "env-20261105-eeeeee": FRESH,
            },
            "kappa-build": {"sindri-registration": OLD},
        }
        self.may_list_folders = False
        self.deleted: list[str] = []
        self.configmap: dict | None = None

    def gitea(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/api/v1/repos/Meru/requests/contents/apps":
            return httpx2.Response(200, json=[{"path": "apps/kappa.yaml", "type": "file"}])
        if path == "/api/v1/repos/Meru/requests/pulls":
            return httpx2.Response(200, json=self.pulls)
        if path == "/api/v1/repos/Meru/requests/pulls/7/files":
            return httpx2.Response(200, json=[{"filename": "apps/kappa.yaml"}])
        if path == "/api/v1/repos/Meru/requests/raw/apps/kappa.yaml":
            return httpx2.Response(200, text=self.pr if request.url.params.get("ref") == "abc123" else self.main)
        if path == "/api/v1/repos/Meru/kappa/raw/deploy/values.yaml":
            return httpx2.Response(404) if self.values is None else httpx2.Response(200, text=self.values)
        if path == "/api/v1/repos/Meru/kappa":
            return httpx2.Response(200, json={"archived": False}) if self.repo_visible else httpx2.Response(404)
        raise AssertionError(path)

    def openbao(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/v1/")
        if path == "auth/kubernetes/login":
            return httpx2.Response(200, json={"auth": {"client_token": "bao-token", "lease_duration": 600}})
        assert request.headers["X-Vault-Token"] == "bao-token"
        rest = path.removeprefix("apps/metadata/meru/")
        if request.method == "DELETE":
            self.deleted.append(rest)
            return httpx2.Response(204)
        if rest == "":
            if not self.may_list_folders:
                return httpx2.Response(403, json={"errors": ["permission denied"]})
            return httpx2.Response(200, json={"data": {"keys": [f"{f}/" for f in self.entries]}})
        folder, _, name = rest.partition("/eso/")
        if name == "":
            if folder not in self.entries:
                return httpx2.Response(404, json={"errors": []})
            return httpx2.Response(200, json={"data": {"keys": list(self.entries[folder])}})
        return httpx2.Response(200, json={"data": {"created_time": self.entries[folder][name], "versions": {}}})

    def cluster(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "PATCH":
            assert request.url.path == "/api/v1/namespaces/charon/configmaps/charon-sweep"
            if self.configmap is None:
                return httpx2.Response(404)
            self.configmap = json.loads(request.content)["data"]
            return httpx2.Response(200, json={})
        assert request.method == "POST"
        self.configmap = json.loads(request.content)["data"]
        return httpx2.Response(201, json={})

    def run(self, delete: bool) -> tuple[Report, int]:
        gitea = httpx2.Client(base_url="https://gitea.test", transport=httpx2.MockTransport(self.gitea))
        openbao = httpx2.Client(base_url="https://openbao.test", transport=httpx2.MockTransport(self.openbao))
        cluster = httpx2.Client(base_url="https://cluster.test", transport=httpx2.MockTransport(self.cluster))
        outside = Outside(
            cluster=Cluster(http=cluster),
            vault=Vault.login(openbao, "meru-charon-purge", "pod-token"),
            registry=Registry(http=httpx2.Client(), user="charon", password=SecretStr("pw")),
            repos=AppRepos(http=gitea, org="Meru"),
            removals=Removals(
                http=gitea, gitea_url="https://gitea.test", repo="Meru/requests", user="charon", token=SecretStr("t")
            ),
        )
        report, failed = sweep(RequestFiles(http=gitea, repo="Meru/requests"), outside, delete, NOW)
        SweepReport(http=cluster, namespace="charon", name="charon-sweep").write(report, NOW)
        return report, failed


@pytest.fixture
def world() -> World:
    return World()


def test_only_an_old_entry_nothing_points_at_is_unreferenced(world: World):
    report, failed = world.run(delete=False)
    assert failed == 0
    assert report.unreferenced == ["kappa/eso/env-20261104-dddddd"]  # main, values, open PR and the fresh one stay
    assert world.deleted == []
    assert report.folders_listed is False


def test_the_switch_on_deletes_it(world: World):
    report, _ = world.run(delete=True)
    assert world.deleted == ["kappa/eso/env-20261104-dddddd"]
    assert report.unreferenced == []
    assert report.deleted == ["kappa/eso/env-20261104-dddddd"]


def test_a_merged_change_frees_the_entry_it_replaced(world: World):
    world.main, world.pulls = world.pr, []
    report, _ = world.run(delete=False)
    assert report.unreferenced == ["kappa/eso/env-20261101-aaaaaa", "kappa/eso/env-20261104-dddddd"]


def test_no_values_file_is_no_wiring(world: World):
    world.values = None
    report, _ = world.run(delete=False)
    assert "kappa/eso/env-20261102-bbbbbb" in report.unreferenced


def test_a_repo_charon_cannot_see_keeps_everything_of_that_app(world: World):
    world.values, world.repo_visible = None, False
    report, failed = world.run(delete=True)
    assert failed == 1
    assert (report.unreferenced, world.deleted) == ([], [])


def test_a_removed_app_is_left_to_its_purge(world: World):
    world.main = request("env-20261101-aaaaaa", removed=True)
    report, _ = world.run(delete=True)
    assert (report.unreferenced, world.deleted) == ([], [])


def test_entries_outside_any_request_count_once_folders_can_be_listed(world: World):
    world.entries["made-up"] = {"env-20261101-ffffff": OLD, "env": OLD}
    report, _ = world.run(delete=False)
    assert "made-up/eso/env-20261101-ffffff" not in report.unreferenced
    world.may_list_folders = True
    report, _ = world.run(delete=False)
    assert report.folders_listed is True
    assert report.unreferenced == ["kappa/eso/env-20261104-dddddd", "made-up/eso/env-20261101-ffffff"]


def test_a_broken_pr_version_keeps_everything_of_that_app(world: World):
    world.pr = "spec: [unclosed"
    report, failed = world.run(delete=True)
    assert failed == 1
    assert (report.unreferenced, world.deleted) == ([], [])


def test_the_report_is_written_then_replaced(world: World):
    world.run(delete=False)
    assert world.configmap is not None
    assert world.configmap["unreferenced"] == "1"
    assert json.loads(world.configmap["unreferenced.json"]) == ["kappa/eso/env-20261104-dddddd"]
    assert world.configmap["folders-listed"] == "false"
    assert world.configmap["checked-at"] == "2026-11-05T12:00:00+00:00"
    world.run(delete=True)
    assert (world.configmap["unreferenced"], world.configmap["deleted"]) == ("0", "1")


def test_an_environments_folder_is_the_apps_and_never_swept(world: World):
    # D126: kappa-prod/ is kappa's prod environment, not a folder without a request
    for side in ("main", "pr"):
        setattr(
            world, side, getattr(world, side).replace("  secrets:", "  environments:\n    - name: prod\n  secrets:")
        )
    world.entries["kappa-prod"] = {"env": OLD, "env-20261101-ffffff": OLD}
    world.may_list_folders = True
    report, failed = world.run(delete=True)
    assert failed == 0
    assert not [e for e in report.unreferenced + report.deleted if e.startswith("kappa-prod/")]
