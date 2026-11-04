"""Purge steps 1 to 4 (D109-D112): a retired app's namespaces, volumes, OpenBao entries and images. Two keys for
a namespace, set by different hands: the request says when it was removed and how long to keep it, and Shikigami's
retire pass marked the namespace with that same moment. A volume has no mark: its second key is the cluster's own,
Released, and that the namespace it belonged to is gone - so a volume always goes at least one pass after its namespace.
The OpenBao entries wait for both of the app's namespaces the same way: nothing in OpenBao can refuse Charon on a live
app, these checks are the only guard. The images wait the same way, and Zot refuses Charon on a repository that still
has its own entry. Anything goes only when every check passes, so a restore, a second retire or a
half-done retire keeps it."""

import logging
from datetime import UTC, datetime

import httpx2
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from charon.cluster import Cluster, Namespace, Volume
from charon.registry import Registry
from charon.request_files import AppRequest, RequestFiles
from charon.vault import Vault

log = logging.getLogger("charon")

READ_ERRORS = (httpx2.HTTPError, ValidationError, yaml.YAMLError)


class Switches(BaseModel):
    """What a pass really does; off = it only says so."""

    model_config = ConfigDict(frozen=True)

    namespaces: bool
    volumes: bool
    secrets: bool
    images: bool


class Outside(BaseModel):
    """The systems a pass acts on."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    cluster: Cluster
    vault: Vault
    registry: Registry


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


def secret_entries(name: str) -> list[str]:
    """What a retired app leaves in OpenBao, the only paths Charon's policy allows."""
    return [f"{name}/eso/env", f"{name}-build/build", f"{name}-build/eso/sindri-registration"]


def purge_secrets(name: str, blocked: list[str], vault: Vault, delete: bool) -> None:
    if found := blocked:
        log.info("%s secrets: kept: %s", name, "; ".join(found))
    elif delete:
        for entry in secret_entries(name):
            vault.delete(entry)
        log.info("%s secrets: deleted", name)
    else:
        log.info("%s secrets: would delete (dry run)", name)


def purge_images(name: str, blocked: list[str], registry: Registry, delete: bool) -> None:
    digests = registry.manifests(name)
    if not digests:
        log.info("%s images: nothing left", name)
    elif blocked:
        log.info("%s images: kept %d: %s", name, len(digests), "; ".join(blocked))
    elif delete:
        for digest in digests:
            registry.delete(name, digest)
        log.info("%s images: deleted %d", name, len(digests))
    else:
        log.info("%s images: would delete %d (dry run)", name, len(digests))


def purge_app(app: AppRequest, outside: Outside, seen: Seen, switches: Switches, now: datetime) -> None:
    name = app.metadata.name
    removed_at = app.metadata.removedAt
    if removed_at is None:
        log.warning("%s: kept: removed, but the request has no removedAt", name)
        return
    owned = (name, f"{name}-build")
    namespaces = [seen.namespaces[n] for n in owned if n in seen.namespaces]
    volumes = [v for v in seen.volumes if v.waiting and v.claim_namespace in owned]
    cluster = outside.cluster
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
    blocked = common + [f"namespace {n} still exists" for n in owned if n in seen.namespaces]
    purge_secrets(name, blocked, outside.vault, switches.secrets)
    purge_images(name, blocked, outside.registry, switches.images)


def purge(files: RequestFiles, outside: Outside, switches: Switches, now: datetime) -> int:
    """One pass over every app request. One app failing doesn't stop the others. Returns how many failed."""
    seen = Seen(namespaces=outside.cluster.namespaces(), volumes=outside.cluster.volumes())
    failed = 0
    for path in files.paths():
        try:
            app = files.read(path)
            if app.spec.removed:
                purge_app(app, outside, seen, switches, now)
        except READ_ERRORS:
            log.exception("%s: failed", path)
            failed += 1
    return failed
