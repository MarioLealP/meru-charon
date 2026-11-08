"""The change-entry sweep (D124): OpenBao keeps only the env-* entries something still points at.

Every secrets change Janus writes is a new entry, env-<date>-<hex> (D123), so old ones pile up. An entry stays while
the app's request on main names it, an open PR's version of that request names it, or the app's deploy/values.yaml
wires it - and for a day after it was made, so a change Janus just wrote isn't swept before its PR exists. Anything
else is unreferenced: counted for Zabbix every pass, deleted only with the switch on. `env`, the onboarding entry, is
never swept. A retired app's entries go with its purge (purge.py), not here.

Folders under apps/meru/ with no request at all - entries made up by a compromised writer - are only seen once
Charon's policy lets it list apps/meru/ (folders() answers None until then).
"""

import json
import logging
from datetime import datetime, timedelta

import httpx2
from pydantic import BaseModel, ConfigDict

from charon.clients.request_files import RequestFiles, parse
from charon.purge.purge import READ_ERRORS, Outside

log = logging.getLogger("charon")

CHANGE_PREFIX = "env-"
GRACE = timedelta(days=1)


class Report(BaseModel):
    """What a sweep pass leaves for Zabbix."""

    model_config = ConfigDict(frozen=True)

    unreferenced: list[str]  # app/entry, still there after this pass
    deleted: list[str]
    folders_listed: bool  # False: entries outside any request are invisible to this pass


class SweepReport(BaseModel):
    """The ConfigMap Zabbix reads (Meru/meru rbac/charon-sweep-report.yaml), the Anubis pattern (D115)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    http: httpx2.Client
    namespace: str
    name: str

    def write(self, report: Report, checked_at: datetime) -> None:
        """Replace what the last pass wrote."""
        data = {
            "unreferenced": str(len(report.unreferenced)),
            "unreferenced.json": json.dumps(report.unreferenced),
            "deleted": str(len(report.deleted)),
            "folders-listed": str(report.folders_listed).lower(),
            "checked-at": checked_at.isoformat(timespec="seconds"),
        }
        path = f"/api/v1/namespaces/{self.namespace}/configmaps"
        response = self.http.patch(
            f"{path}/{self.name}",
            headers={"Content-Type": "application/merge-patch+json"},
            content=json.dumps({"data": data}),
        )
        if response.status_code == 404:  # the first pass
            response = self.http.post(path, json={"metadata": {"name": self.name}, "data": data})
        response.raise_for_status()


def kept(app: str, path: str, text: str, pending: dict[str, list[str]], outside: Outside) -> set[str] | None:
    """The entries something points at for one app, or None for a removed app (its purge takes them)."""
    request = parse(text)
    if request.spec.removed:
        return None
    refs = set(request.entries) | outside.repos.wired_entries(app)
    for version in pending.get(path, []):
        refs |= parse(version).entries
    return refs


def unreferenced(folder: str, refs: set[str], outside: Outside, now: datetime) -> list[str]:
    """The folder's change entries nothing points at and older than a day."""
    out = []
    for name in outside.vault.names(folder):
        if not name.startswith(CHANGE_PREFIX) or name in refs:
            continue
        if now - outside.vault.created(f"{folder}/eso/{name}") >= GRACE:
            out.append(f"{folder}/eso/{name}")
    return out


def sweep(files: RequestFiles, outside: Outside, delete: bool, now: datetime) -> tuple[Report, int]:
    """One pass over every app's change entries. Returns the report and how many apps failed."""
    pending = files.open_versions()
    found: list[str] = []
    seen: set[str] = set()
    failed = 0
    for path in files.paths():
        app = path.removeprefix("apps/").removesuffix(".yaml")  # the request's own name (Shikigami checks they match)
        seen.add(app)
        try:
            text = files.raw(path)
            # D126: its environments' folders are the app's too - never "no request". Not swept yet either: what
            # points at an environment's change entries comes with per-environment secrets.
            seen.update(parse(text).namespaces[2:])
            refs = kept(app, path, text, pending, outside)
            if refs is not None:
                found += unreferenced(app, refs, outside, now)
        except READ_ERRORS:
            log.exception("%s sweep: failed", path)
            failed += 1
    folders = outside.vault.folders()
    for folder in folders or []:
        app = folder.removesuffix("-build")
        if app not in seen and folder not in seen:  # no request names it: nothing can point at its entries
            found += unreferenced(folder, set(), outside, now)
    deleted = []
    for entry in found:
        if delete:
            outside.vault.delete(entry)
            deleted.append(entry)
            log.info("%s: deleted (unreferenced)", entry)
        else:
            log.info("%s: unreferenced, would delete (dry run)", entry)
    left = [entry for entry in found if entry not in deleted]
    return Report(unreferenced=left, deleted=deleted, folders_listed=folders is not None), failed
