"""The app requests on Meru/requests' main (D83: Charon finds its work there), only the fields a purge decides on.

Read loosely on purpose: Shikigami's gate has already checked the files, and a new field must not stop the purge.
"""

from datetime import timedelta
from typing import Annotated

import httpx2
import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from charon.incoming import Incoming

Days = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,3}d$")]


class _Metadata(Incoming):
    name: str
    removed_at: AwareDatetime | None = Field(default=None, alias="removedAt")


class _Secret(Incoming):
    name: str
    entry: str = "env"  # the OpenBao entry holding it (D123)


class _Spec(Incoming):
    removed: bool = False
    retention: Days = "30d"  # the same default as the request format
    secrets: list[_Secret] = Field(default_factory=list)


class AppRequest(Incoming):
    """One app's request file."""

    metadata: _Metadata
    spec: _Spec

    @property
    def retention(self) -> timedelta:
        """How long after its removal the app is kept."""
        return timedelta(days=int(self.spec.retention.removesuffix("d")))

    @property
    def entries(self) -> set[str]:
        """The OpenBao entries its secrets live in (D123)."""
        return {secret.entry for secret in self.spec.secrets}


class _Entry(Incoming):
    path: str
    type: str


class _Head(Incoming):
    sha: str


class _Pull(Incoming):
    number: int
    head: _Head


class _File(Incoming):
    filename: str


class RequestFiles(BaseModel):
    """Meru/requests on Gitea, with the bot that has only Read on it (D119)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    http: httpx2.Client
    repo: str

    def paths(self) -> list[str]:
        """Every app request on main."""
        response = self.http.get(f"/api/v1/repos/{self.repo}/contents/apps")
        response.raise_for_status()
        entries = [_Entry.model_validate(entry) for entry in response.json()]
        return [entry.path for entry in entries if entry.type == "file" and entry.path.endswith(".yaml")]

    def raw(self, path: str, ref: str | None = None) -> str:
        """One app request's text, as main holds it (or another commit) - what a removal PR must still find there."""
        response = self.http.get(f"/api/v1/repos/{self.repo}/raw/{path}", params={"ref": ref} if ref else None)
        response.raise_for_status()
        return response.text

    def open_versions(self) -> dict[str, list[str]]:
        """Each request file an open PR changes -> its text in those PRs: a change on its way in (D124)."""
        api = f"/api/v1/repos/{self.repo}/pulls"
        response = self.http.get(api, params={"state": "open", "limit": 50})
        response.raise_for_status()
        out: dict[str, list[str]] = {}
        for pull in (_Pull.model_validate(p) for p in response.json()):
            files = self.http.get(f"{api}/{pull.number}/files")
            files.raise_for_status()
            for name in (_File.model_validate(f).filename for f in files.json()):
                if name.startswith("apps/"):
                    try:
                        out.setdefault(name, []).append(self.raw(name, pull.head.sha))
                    except httpx2.HTTPStatusError:  # the PR deletes the file: nothing to keep for it
                        continue
        return out


def parse(text: str) -> AppRequest:
    """One app request, validated."""
    return AppRequest.model_validate(yaml.safe_load(text))
