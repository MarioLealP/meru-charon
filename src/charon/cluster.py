"""What Charon reads and changes in the cluster, with the pod's own token. Its rights are in Meru/meru rbac/: list and
delete namespaces (an admission rule refuses any without Meru's label and the removed-at mark), list PersistentVolumes
and set a Released one to Delete (nothing else, by another rule), and `get` on Argo Applications."""

import json

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


class _ClaimRef(Incoming):
    namespace: str


class _HostPath(Incoming):
    path: str


class _VolumeSpec(Incoming):
    claimRef: _ClaimRef | None = None
    hostPath: _HostPath | None = None
    persistentVolumeReclaimPolicy: str


class _VolumeMetadata(Incoming):
    name: str
    uid: str


class _VolumeStatus(Incoming):
    phase: str


class Volume(Incoming):
    metadata: _VolumeMetadata
    spec: _VolumeSpec
    status: _VolumeStatus

    @property
    def path(self) -> str | None:
        return self.spec.hostPath.path if self.spec.hostPath else None

    @property
    def claim_namespace(self) -> str | None:
        return self.spec.claimRef.namespace if self.spec.claimRef else None

    @property
    def waiting(self) -> bool:
        """Released and still kept: what Charon may let go."""
        return self.status.phase == "Released" and self.spec.persistentVolumeReclaimPolicy == "Retain"


class _VolumeList(Incoming):
    items: list[Volume]


class Cluster:
    def __init__(self, http: httpx2.Client) -> None:
        self._http = http

    def namespaces(self) -> dict[str, Namespace]:
        """Meru's namespaces by name. Anything else in the cluster is never looked at."""
        response = self._http.get("/api/v1/namespaces", params={"labelSelector": MERU_NAMESPACES})
        response.raise_for_status()
        return {item.metadata.name: item for item in _NamespaceList.model_validate_json(response.content).items}

    def volumes(self) -> list[Volume]:
        response = self._http.get("/api/v1/persistentvolumes")
        response.raise_for_status()
        return _VolumeList.model_validate_json(response.content).items

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

    def release(self, volume: Volume) -> None:
        """Set to Delete: the provisioner then removes the folder and the PV (D110). The uid, as for a namespace."""
        response = self._http.patch(
            f"/api/v1/persistentvolumes/{volume.metadata.name}",
            headers={"Content-Type": "application/merge-patch+json"},
            content=json.dumps(
                {"metadata": {"uid": volume.metadata.uid}, "spec": {"persistentVolumeReclaimPolicy": "Delete"}}
            ),
        )
        response.raise_for_status()
