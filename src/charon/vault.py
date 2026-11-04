"""OpenBao, with Charon's own login (D111).

The pod's token for role meru-charon-purge, nothing but delete on the three entries a retired app leaves. It can't read
or list, so it never sees a value and can't tell if an entry exists. delete on metadata/ removes an entry and all its
versions for good, and answers the same for one that is already gone.
"""

from typing import Self

import httpx2
from pydantic import BaseModel, ConfigDict, SecretStr

from charon.incoming import Incoming


class _Auth(Incoming):
    client_token: SecretStr


class _Login(Incoming):
    auth: _Auth


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

    def delete(self, entry: str) -> None:
        """Remove one entry and all its versions."""
        headers = {"X-Vault-Token": self.token.get_secret_value()}
        response = self.http.delete(f"/v1/apps/metadata/meru/{entry}", headers=headers)
        response.raise_for_status()
