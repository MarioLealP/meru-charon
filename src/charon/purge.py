"""Purge steps 1 and 2 (D109, D110): a retired app's namespaces, then its volumes. Two keys for a namespace, set by
different hands: the request says when it was removed and how long to keep it, and Shikigami's retire pass marked the
namespace with that same moment. A volume has no mark: its second key is the cluster's own, Released, and that the
namespace it belonged to is gone - so a volume always goes at least one pass after its namespace. Anything goes only
when every check passes, so a restore, a second retire or a half-done retire keeps it."""

import logging
from datetime import UTC, datetime

import httpx2
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from charon.cluster import Cluster, Namespace, Volume
from charon.request_files import AppRequest, RequestFiles

log = logging.getLogger("charon")

READ_ERRORS = (httpx2.HTTPError, ValidationError, yaml.YAMLError)


class Switches(BaseModel):
    """What a pass really does; off = it only says so."""

    model_config = ConfigDict(frozen=True)

    namespaces: bool
    volumes: bool


class Seen(BaseModel):
    """The cluster at the start of a pass."""

    model_config = ConfigDict(frozen=True)

    namespaces: dict[str, Namespace]
    volumes: list[Volume]


def stamp(moment: datetime) -> str:
    """The mark's format: UTC, to the second, as Shikigami writes it from removedAt."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def app_blockers(app: AppRequest, removed_at: datetime, argo_app: bool, now: datetime) -> list[str]:
    """What keeps everything of this app. All of them, not the first: a dry run shows what is still in the way."""
    found = []
    until = removed_at + app.retention
    if now < until:
        found.append(f"retention runs to {stamp(until)}")
    if argo_app:
        found.append("its Argo Application still exists")
    return found


def namespace_blockers(namespace: Namespace, removed_at: datetime) -> list[str]:
    if namespace.removed_at is None:
        return ["no removed-at mark"]
    if namespace.removed_at != stamp(removed_at):
        return [f"mark {namespace.removed_at} isn't the request's {stamp(removed_at)}"]
    return []


def volume_blockers(volume: Volume, seen: Seen) -> list[str]:
    """A restored app's new PV lands in the same folder as its old Released ones, so the path decides, not the PV."""
    found = []
    if volume.claim_namespace in seen.namespaces:
        found.append(f"namespace {volume.claim_namespace} still exists")
    if volume.path is None:
        found.append("no host path to check")
    elif any(other.path == volume.path and other.status.phase != "Released" for other in seen.volumes):
        found.append(f"a volume in use shares {volume.path}")
    return found


def purge_app(app: AppRequest, cluster: Cluster, seen: Seen, switches: Switches, now: datetime) -> None:
    name = app.metadata.name
    removed_at = app.metadata.removedAt
    if removed_at is None:
        log.warning("%s: kept: removed, but the request has no removedAt", name)
        return
    owned = (name, f"{name}-build")
    namespaces = [seen.namespaces[n] for n in owned if n in seen.namespaces]
    volumes = [v for v in seen.volumes if v.waiting and v.claim_namespace in owned]
    if not namespaces and not volumes:
        log.info("%s: nothing left", name)
        return
    common = app_blockers(app, removed_at, cluster.has_application(name), now)
    for namespace in namespaces:
        target = namespace.metadata.name
        if found := common + namespace_blockers(namespace, removed_at):
            log.info("%s: kept: %s", target, "; ".join(found))
        elif switches.namespaces:
            cluster.delete(namespace)
            log.info("%s: deleted", target)
        else:
            log.info("%s: would delete (dry run)", target)
    for volume in volumes:
        target = f"{volume.claim_namespace} volume {volume.metadata.name}"
        if found := common + volume_blockers(volume, seen):
            log.info("%s: kept: %s", target, "; ".join(found))
        elif switches.volumes:
            cluster.release(volume)
            log.info("%s: released", target)
        else:
            log.info("%s: would release (dry run)", target)


def purge(files: RequestFiles, cluster: Cluster, switches: Switches, now: datetime) -> int:
    """One pass over every app request. One app failing doesn't stop the others. Returns how many failed."""
    seen = Seen(namespaces=cluster.namespaces(), volumes=cluster.volumes())
    failed = 0
    for path in files.paths():
        try:
            app = files.read(path)
            if app.spec.removed:
                purge_app(app, cluster, seen, switches, now)
        except READ_ERRORS:
            log.exception("%s: failed", path)
            failed += 1
    return failed
