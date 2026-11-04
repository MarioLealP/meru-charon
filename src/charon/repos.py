"""An app's repo on Gitea, read anonymously (Charon's bot sees only Meru/requests).

Archiving needs admin on the repo, which Gitea can't narrow, so Mario clicks it and Charon only reminds him (D113). The
runner records stay for the same reason. An unarchived retired repo is harmless: it has no runner, no Zot login and no
push rule.
"""

import httpx2
from pydantic import BaseModel, ConfigDict

from charon.incoming import Incoming


class _Repo(Incoming):
    archived: bool


class AppRepos(BaseModel):
    """The apps' repos on Gitea, read without a login."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    http: httpx2.Client
    org: str

    def not_archived(self, app: str) -> bool:
        """Whether the app's repo exists and is still open."""
        response = self.http.get(f"/api/v1/repos/{self.org}/{app}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return not _Repo.model_validate_json(response.content).archived
