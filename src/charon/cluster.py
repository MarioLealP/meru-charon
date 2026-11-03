"""What Charon reads and deletes in the cluster, with the pod's own token. Its rights are in Meru/meru rbac/: list and
delete namespaces, an admission rule refusing any namespace without Meru's label and the removed-at mark, and `get`
on Argo Applications."""

import httpx2
from pydantic import Field

from charon.incoming import Incoming

MERU_NAMESPACES = "app.kubernetes.io/part-of=meru"
REMOVED_AT = "meru.clockmost.com/removed-at"


class _Metadata(Incoming):
    name: str
    uid: str
    annotations: dict[str, str] = Field(default_factory=dict)


class Namespace(Incoming):
    metadata: _Metadata

    @property
    def removed_at(self) -> str | None:
        return self.metadata.annotations.get(REMOVED_AT)


class _NamespaceList(Incoming):
    items: list[Namespace]


class Cluster:
    def __init__(self, http: httpx2.Client) -> None:
        self._http = http

    def namespaces(self) -> dict[str, Namespace]:
        """Meru's namespaces by name. Anything else in the cluster is never looked at."""
        response = self._http.get("/api/v1/namespaces", params={"labelSelector": MERU_NAMESPACES})
        response.raise_for_status()
        return {item.metadata.name: item for item in _NamespaceList.model_validate_json(response.content).items}

    def has_application(self, app: str) -> bool:
        response = self._http.get(f"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/app-{app}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    def delete(self, namespace: Namespace) -> None:
        """Only the namespace that was checked: the uid makes the API refuse a new one with the same name."""
        response = self._http.request(
            "DELETE",
            f"/api/v1/namespaces/{namespace.metadata.name}",
            json={"preconditions": {"uid": namespace.metadata.uid}},
        )
        response.raise_for_status()
