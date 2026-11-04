"""OpenBao, with Charon's own login (D111): the pod's token for role meru-charon-purge, nothing but delete on the three
entries a retired app leaves. It can't read or list, so it never sees a value and can't tell if an entry exists. delete
on metadata/ removes an entry and all its versions for good, and answers the same for one that is already gone."""

import httpx2

from charon.incoming import Incoming


class _Auth(Incoming):
    client_token: str


class _Login(Incoming):
    auth: _Auth


class Vault:
    def __init__(self, http: httpx2.Client, role: str, jwt: str) -> None:
        """Logs in right away, so a pass that can't reach OpenBao fails even with the switch off."""
        response = http.post("/v1/auth/kubernetes/login", json={"role": role, "jwt": jwt})
        response.raise_for_status()
        self._http = http
        self._token = _Login.model_validate_json(response.content).auth.client_token

    def delete(self, entry: str) -> None:
        response = self._http.delete(f"/v1/apps/metadata/meru/{entry}", headers={"X-Vault-Token": self._token})
        response.raise_for_status()
