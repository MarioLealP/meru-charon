"""Zot, for a retired app's images (D112).

Reads are anonymous; the `charon` login goes only on the delete. Its one rule, meru/**, reaches only repositories with
no entry of their own: a live app has one, so Zot itself refuses Charon there and the pass fails. Deleting the manifests
is all Charon does - Zot's garbage collection clears the blobs.
"""

import httpx2
from pydantic import BaseModel, ConfigDict, SecretStr

from charon.incoming import Incoming

MANIFESTS = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class _Tags(Incoming):
    tags: list[str] | None = None


class Registry(BaseModel):
    """Zot's API: anonymous reads, the login only on a delete."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    http: httpx2.Client
    user: str
    password: SecretStr

    def manifests(self, app: str) -> list[str]:
        """The digest behind each tag of meru/<app>, signatures included. None left: Zot no longer knows the repo."""
        response = self.http.get(f"/v2/meru/{app}/tags/list")
        if response.status_code == 404:
            return []
        response.raise_for_status()
        digests = [self._digest(app, tag) for tag in _Tags.model_validate_json(response.content).tags or []]
        return list(dict.fromkeys(digests))

    def _digest(self, app: str, tag: str) -> str:
        response = self.http.head(f"/v2/meru/{app}/manifests/{tag}", headers={"Accept": MANIFESTS})
        response.raise_for_status()
        return response.headers["Docker-Content-Digest"]

    def delete(self, app: str, digest: str) -> None:
        """Delete one manifest of meru/<app>. Zot refusing fails the app's purge."""
        response = self.http.delete(
            f"/v2/meru/{app}/manifests/{digest}", auth=(self.user, self.password.get_secret_value())
        )
        response.raise_for_status()
