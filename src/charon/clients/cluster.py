"""What Charon reads and changes in the cluster, with the pod's own token.

Its rights are in Meru/meru rbac/: list and delete namespaces (an admission rule refuses any without Meru's label and
the removed-at mark), list PersistentVolumes and set a Released one to Delete (nothing else, by another rule), and
`get` on Argo Applications.
"""

import json

import httpx2
from pydantic import BaseModel, ConfigDict, Field

from charon.models.incoming import Incoming

MERU_NAMESPACES = "app.kubernetes.io/part-of=meru"
REMOVED_AT = "meru.clockmost.com/removed-at"


class _Metadata(Incoming):
    name: str
    uid: str
    annotations: dict[str, str] = Field(default_factory=dict)


class Namespace(Incoming):
    """A Meru namespace, as the cluster lists it."""

    metadata: _Metadata

    @property
    def removed_at(self) -> str | None:
        """The removed-at mark Shikigami's retire pass left, if any."""
        return self.metadata.annotations.get(REMOVED_AT)


class _NamespaceList(Incoming):
    items: list[Namespace]


class _ClaimRef(Incoming):
    namespace: str


class _HostPath(Incoming):
    path: str


class _VolumeSpec(Incoming):
    claim_ref: _ClaimRef | None = Field(default=None, alias="claimRef")
    host_path: _HostPath | None = Field(default=None, alias="hostPath")
    persistent_volume_reclaim_policy: str = Field(alias="persistentVolumeReclaimPolicy")


class _VolumeMetadata(Incoming):
    name: str
    uid: str


class _VolumeStatus(Incoming):
    phase: str


class Volume(Incoming):
    """A PersistentVolume, as the cluster lists it."""

    metadata: _VolumeMetadata
    spec: _VolumeSpec
    status: _VolumeStatus

    @property
    def path(self) -> str | None:
        """The folder on the host, if it has one."""
        return self.spec.host_path.path if self.spec.host_path else None

    @property
    def claim_namespace(self) -> str | None:
        """The namespace of the claim it was bound to, if any."""
        return self.spec.claim_ref.namespace if self.spec.claim_ref else None

    @property
    def waiting(self) -> bool:
        """Released and still kept: what Charon may let go."""
        return self.status.phase == "Released" and self.spec.persistent_volume_reclaim_policy == "Retain"


class _VolumeList(Incoming):
    items: list[Volume]


class Cluster(BaseModel):
    """The Kubernetes API, with the pod's own token."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    http: httpx2.Client

    def namespaces(self) -> dict[str, Namespace]:
        """Meru's namespaces by name. Anything else in the cluster is never looked at."""
        response = self.http.get("/api/v1/namespaces", params={"labelSelector": MERU_NAMESPACES})
        response.raise_for_status()
        return {item.metadata.name: item for item in _NamespaceList.model_validate_json(response.content).items}

    def volumes(self) -> list[Volume]:
        """Every PersistentVolume in the cluster."""
        response = self.http.get("/api/v1/persistentvolumes")
        response.raise_for_status()
        return _VolumeList.model_validate_json(response.content).items

    def has_application(self, app: str) -> bool:
        """Whether the app's Argo Application still exists."""
        response = self.http.get(f"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/app-{app}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    def delete(self, namespace: Namespace) -> None:
        """Only the namespace that was checked: the uid makes the API refuse a new one with the same name."""
        response = self.http.request(
            "DELETE",
            f"/api/v1/namespaces/{namespace.metadata.name}",
            json={"preconditions": {"uid": namespace.metadata.uid}},
        )
        response.raise_for_status()

    def release(self, volume: Volume) -> None:
        """Set to Delete: the provisioner then removes the folder and the PV (D110). The uid, as for a namespace."""
        response = self.http.patch(
            f"/api/v1/persistentvolumes/{volume.metadata.name}",
            headers={"Content-Type": "application/merge-patch+json"},
            content=json.dumps(
                {"metadata": {"uid": volume.metadata.uid}, "spec": {"persistentVolumeReclaimPolicy": "Delete"}}
            ),
        )
        response.raise_for_status()
