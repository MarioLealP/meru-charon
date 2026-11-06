"""OpenBao, with Charon's own login (D111, D124).

The pod's token for role meru-charon-purge: delete on the entries a retired app leaves and on change entries (env-*),
list on an app's eso/ names, read on change entries' metadata (when they were made). It never sees a value. delete on
metadata/ removes an entry and all its versions for good, and answers the same for one that is already gone.
"""

from datetime import datetime
from typing import Self

import httpx2
from pydantic import BaseModel, ConfigDict, SecretStr

from charon.models.incoming import Incoming


class _Auth(Incoming):
    client_token: SecretStr


class _Login(Incoming):
    auth: _Auth


class _Keys(Incoming):
    keys: list[str]


class _Listed(Incoming):
    data: _Keys


class _Metadata(Incoming):
    created_time: datetime


class _MetadataAnswer(Incoming):
    data: _Metadata


class Vault(BaseModel):
    """OpenBao's API, logged in as Charon's role."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    http: httpx2.Client
    token: SecretStr

    @classmethod
    def login(cls, http: httpx2.Client, role: str, jwt: str) -> Self:
        """Log in. Called before the pass starts, so one that can't reach OpenBao fails even with the switch off."""
        response = http.post("/v1/auth/kubernetes/login", json={"role": role, "jwt": jwt})
        response.raise_for_status()
        return cls(http=http, token=_Login.model_validate_json(response.content).auth.client_token)

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Vault-Token": self.token.get_secret_value()}

    def delete(self, entry: str) -> None:
        """Remove one entry and all its versions."""
        response = self.http.delete(f"/v1/apps/metadata/meru/{entry}", headers=self._headers)
        response.raise_for_status()

    def names(self, folder: str) -> list[str]:
        """The entry names under one folder's eso/ - names only (D124). None there: an empty list."""
        response = self.http.get(
            f"/v1/apps/metadata/meru/{folder}/eso/", params={"list": "true"}, headers=self._headers
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return [key for key in _Listed.model_validate_json(response.content).data.keys if not key.endswith("/")]

    def created(self, entry: str) -> datetime:
        """When an entry was first written (its metadata, never its value)."""
        response = self.http.get(f"/v1/apps/metadata/meru/{entry}", headers=self._headers)
        response.raise_for_status()
        return _MetadataAnswer.model_validate_json(response.content).data.created_time

    def folders(self) -> list[str] | None:
        """Every folder under apps/meru/, or None while Charon's policy doesn't allow listing them."""
        response = self.http.get("/v1/apps/metadata/meru/", params={"list": "true"}, headers=self._headers)
        if response.status_code == 403:
            return None
        response.raise_for_status()
        return [key.removesuffix("/") for key in _Listed.model_validate_json(response.content).data.keys]
