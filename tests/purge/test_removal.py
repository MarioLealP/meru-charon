"""The git half of step 5 against a real (local, bare) repo - Gitea's AGit isn't there, plain refs/for/main is."""

import subprocess
from pathlib import Path

import httpx2
import pytest
from pydantic import SecretStr

from charon.purge.removal import ProposalFailedError, Removals, title

REQUEST = "apiVersion: meru.clockmost.com/v1\nkind: App\nmetadata:\n  name: tanuki\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """Meru/requests with apps/tanuki.yaml and one other request on main."""
    bare = tmp_path / "Meru" / "requests.git"
    bare.mkdir(parents=True)
    git(bare, "init", "--quiet", "--bare", "--initial-branch", "main")
    git(bare, "config", "receive.advertisePushOptions", "true")
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "--quiet", str(bare), str(seed))
    (seed / "apps").mkdir()
    (seed / "apps" / "tanuki.yaml").write_text(REQUEST)
    (seed / "apps" / "golem.yaml").write_text("other\n")
    git(seed, "add", ".")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "seed")
    git(seed, "push", "--quiet", "origin", "HEAD:main")
    return bare


def removals(remote: Path) -> Removals:
    root = remote.parent.parent
    return Removals(
        http=httpx2.Client(), gitea_url=f"file://{root}", repo="Meru/requests", user="charon", token=SecretStr("t")
    )


def test_the_pr_deletes_only_that_request(remote: Path):
    removals(remote).propose("tanuki", "apps/tanuki.yaml", REQUEST)
    assert git(remote, "log", "-1", "--format=%s%n%an", "refs/for/main") == f"{title('tanuki')}\nCharon (Meru purge)"
    assert git(remote, "diff", "--name-status", "main", "refs/for/main") == "D\tapps/tanuki.yaml"


def test_a_request_changed_on_main_since_it_was_read_is_not_proposed(remote: Path):
    with pytest.raises(ProposalFailedError, match="changed on main"):
        removals(remote).propose("tanuki", "apps/tanuki.yaml", REQUEST + "  removedAt: earlier\n")
    with pytest.raises(subprocess.CalledProcessError):
        git(remote, "rev-parse", "--verify", "refs/for/main")
