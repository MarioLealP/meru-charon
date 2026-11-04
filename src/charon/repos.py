"""An app's repo on Gitea, read anonymously (Charon's bot sees only Meru/requests). Archiving needs admin on the repo,
which Gitea can't narrow, so Mario clicks it and Charon only reminds him (D113). The runner records stay for the same
reason. An unarchived retired repo is harmless: it has no runner, no Zot login and no push rule."""

import httpx2

from charon.incoming import Incoming


class _Repo(Incoming):
    archived: bool


class AppRepos:
    def __init__(self, http: httpx2.Client, org: str) -> None:
        self._http = http
        self._org = org

    def not_archived(self, app: str) -> bool:
        response = self._http.get(f"/api/v1/repos/{self._org}/{app}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return not _Repo.model_validate_json(response.content).archived
