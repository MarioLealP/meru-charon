"""Purge step 5 (cm-mr2a): once nothing of a retired app is left, propose deleting its request file.

An AGit PR into Meru/requests - `git push origin HEAD:refs/for/main` - which Gitea accepts from a reader: Charon's bot
has only Read on the repo, so a leaked token can propose, never change `main` (D119). Xolotl merges it only if it
deletes exactly `apps/<app>.yaml` of a removed app whose retention has run out by its own clock; anything else
quarantines Charon. Once the file is gone, Anubis gives the app the `retired` verdict (cm-g8bt).
"""

import base64
import os
import subprocess
import tempfile
from pathlib import Path

import httpx2
from pydantic import BaseModel, ConfigDict, SecretStr, TypeAdapter

from charon.incoming import Incoming

AUTHOR = ("Charon (Meru purge)", "charon@clockmost.local")


def title(app: str) -> str:
    """The only title Xolotl merges a deletion under - the shared contract with xolotl's purged.py."""
    return f"chore(purge): delete {app} request"


class ProposalFailedError(Exception):
    """git refused, or main no longer holds the request as read (the message is git's, never the token)."""


class _Pull(Incoming):
    title: str


_PULLS = TypeAdapter(list[_Pull])


class Removals(BaseModel):
    """Meru/requests' PRs, with Charon's bot: write:repository scope, Read rights (D119)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    http: httpx2.Client  # the bot's API client
    gitea_url: str
    repo: str
    user: str
    token: SecretStr

    def pending(self, app: str) -> bool:
        """Whether Charon's PR for this app is already open: one PR per app, not one per pass."""
        response = self.http.get(f"/api/v1/repos/{self.repo}/pulls", params={"state": "open", "limit": 50})
        response.raise_for_status()
        return any(pr.title == title(app) for pr in _PULLS.validate_json(response.content))

    def propose(self, app: str, path: str, before: str) -> None:
        """One commit deleting `path` on top of main, pushed as an AGit PR - only if main still holds `before`."""
        auth = base64.b64encode(f"{self.user}:{self.token.get_secret_value()}".encode()).decode()
        with tempfile.TemporaryDirectory(prefix="charon-pr-") as tmp:
            work = Path(tmp) / "repo"
            _git(Path(tmp), auth, "clone", "--quiet", "--depth", "1", "--branch", "main", self._remote, str(work))
            target = work / path
            if not target.is_file() or target.read_text(encoding="utf-8") != before:
                raise ProposalFailedError(f"{path} changed on main since it was read")
            _git(work, auth, "rm", "--quiet", "--", path)
            name, email = AUTHOR
            _git(
                work,
                auth,
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "commit",
                "--quiet",
                "-m",
                title(app),
            )
            _git(
                work,
                auth,
                "push",
                "--quiet",
                "origin",
                "HEAD:refs/for/main",
                "-o",
                f"topic=purge-{app}",
                "-o",
                f"title={title(app)}",
            )

    @property
    def _remote(self) -> str:
        return f"{self.gitea_url}/{self.repo}.git"


def _git(cwd: Path, auth: str, *args: str) -> None:
    """Git with no user or system config, and the login in a header so it never lands in a URL or a message."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(cwd),  # the root filesystem is read-only
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}",
        },
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise ProposalFailedError(f"git {args[0]}: {result.stderr.decode(errors='replace').strip()[-500:]}")
