"""An app's repo on Gitea: read anonymously, and with the bot's login when the anonymous read answers 404.

Charon's bot is a restricted Gitea user: it sees no public repo it isn't added to, so the public app repos are read
without a login (D124). A private app repo (nova-lemuria) answers an anonymous read with 404; the bot has Read on
exactly those (declared in Meru/tofu `charon-reads-private-apps.tf`), so the second try, with its login, gets through.
A repo that is gone, or private and not granted, still 404s both ways and raises where the sweep needs a verdict.

Archiving needs admin on the repo, which Gitea can't narrow, so Mario clicks it and Charon only reminds him (D113). The
runner records stay for the same reason. An unarchived retired repo is harmless: it has no runner, no Zot login and no
push rule.
"""

import httpx2
import yaml
from pydantic import BaseModel, ConfigDict

from charon.models.incoming import Incoming


class _Repo(Incoming):
    archived: bool


class AppRepos(BaseModel):
    """The apps' repos on Gitea, read without a login first."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    http: httpx2.Client
    org: str
    login: httpx2.Client | None = None  # the bot's own client: the second try after an anonymous 404

    def _get(self, path: str) -> httpx2.Response:
        response = self.http.get(path)
        if response.status_code == 404 and self.login is not None:
            return self.login.get(path)
        return response

    def not_archived(self, app: str) -> bool:
        """Whether the app's repo exists and is still open."""
        response = self._get(f"/api/v1/repos/{self.org}/{app}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return not _Repo.model_validate_json(response.content).archived

    def wired_entries(self, app: str) -> set[str]:
        """The externalSecrets the app's deploy/values.yaml on main names - what the running app reads (D124).

        A 404 means no such file only while the repo itself answers: a repo Charon can't see (private, gone) raises,
        so the sweep keeps that app's entries instead of reading "nothing wired".
        """
        response = self._get(f"/api/v1/repos/{self.org}/{app}/raw/deploy/values.yaml")
        if response.status_code == 404:
            self._get(f"/api/v1/repos/{self.org}/{app}").raise_for_status()
            return set()
        response.raise_for_status()
        wired = (yaml.safe_load(response.text) or {}).get("externalSecrets") or {}
        return set(wired) if isinstance(wired, dict) else set()
