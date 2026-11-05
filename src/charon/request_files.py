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


class _Spec(Incoming):
    removed: bool = False
    retention: Days = "30d"  # the same default as the request format


class AppRequest(Incoming):
    """One app's request file."""

    metadata: _Metadata
    spec: _Spec

    @property
    def retention(self) -> timedelta:
        """How long after its removal the app is kept."""
        return timedelta(days=int(self.spec.retention.removesuffix("d")))


class _Entry(Incoming):
    path: str
    type: str


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

    def raw(self, path: str) -> str:
        """One app request's text, as main holds it - what a removal PR must still find there."""
        response = self.http.get(f"/api/v1/repos/{self.repo}/raw/{path}")
        response.raise_for_status()
        return response.text


def parse(text: str) -> AppRequest:
    """One app request, validated."""
    return AppRequest.model_validate(yaml.safe_load(text))
