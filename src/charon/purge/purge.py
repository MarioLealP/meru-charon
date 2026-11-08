"""Purge steps 1 to 4 (D109-D112): a retired app's namespaces, volumes, OpenBao entries and images.

Two keys for a namespace, set by different hands: the request says when it was removed and how long to keep it, and
Shikigami's retire pass marked the namespace with that same moment. A volume has no mark: its second key is the
cluster's own, Released, and that the namespace it belonged to is gone - so a volume always goes at least one pass after
its namespace. The OpenBao entries wait for both of the app's namespaces the same way: nothing in OpenBao can refuse
Charon on a live app, these checks are the only guard. The images wait the same way, and Zot refuses Charon on a
repository that still has its own entry. Anything goes only when every check passes, so a restore, a second retire or a
half-done retire keeps it.

Step 5 (removal.py): when a pass finds nothing of the app left - no namespace, no volume, the entries deleted, no image,
the repo archived - it proposes deleting the request file.
"""

import logging
from datetime import UTC, datetime

import httpx2
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from charon.clients.cluster import Cluster, Namespace, Volume
from charon.clients.registry import Registry
from charon.clients.repos import AppRepos
from charon.clients.request_files import AppRequest, RequestFiles, parse
from charon.clients.vault import Vault
from charon.purge.removal import ProposalFailedError, Removals

log = logging.getLogger("charon")

READ_ERRORS = (httpx2.HTTPError, ValidationError, yaml.YAMLError, ProposalFailedError)


class Switches(BaseModel):
    """What a pass really does; off = it only says so."""

    model_config = ConfigDict(frozen=True)

    namespaces: bool
    volumes: bool
    secrets: bool
    images: bool
    removal: bool


class Outside(BaseModel):
    """The systems a pass acts on."""

    model_config = ConfigDict(frozen=True)

    cluster: Cluster
    vault: Vault
    registry: Registry
    repos: AppRepos
    removals: Removals


class Seen(BaseModel):
    """The cluster at the start of a pass."""

    model_config = ConfigDict(frozen=True)

    namespaces: dict[str, Namespace]
    volumes: list[Volume]


def stamp(moment: datetime) -> str:
    """The mark's format: UTC, to the second, as Shikigami writes it from removedAt."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def app_blockers(app: AppRequest, removed_at: datetime, argo_apps: list[str], now: datetime) -> list[str]:
    """What keeps everything of this app. All of them, not the first: a dry run shows what is still in the way.

    `argo_apps`: its Applications that still exist - the app's own or an environment's (D126).
    """
    found = []
    until = removed_at + app.retention
    if now < until:
        found.append(f"retention runs to {stamp(until)}")
    found += [f"its Argo Application {name} still exists" for name in argo_apps]
    return found


def namespace_blockers(namespace: Namespace, removed_at: datetime) -> list[str]:
    """What keeps this namespace: its mark has to be the request's own moment."""
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


def secret_entries(name: str, environments: tuple[str, ...] = ()) -> list[str]:
    """What a retired app leaves in OpenBao, the only paths Charon's policy allows.

    `environments`: the <app>-<env> namespaces (D126) - each reads its own apps/meru/<namespace>/eso/env.
    """
    return [
        f"{name}/eso/env",
        f"{name}-build/build",
        f"{name}-build/eso/sindri-registration",
        *(f"{ns}/eso/env" for ns in environments),
    ]


def purge_secrets(
    name: str, blocked: list[str], vault: Vault, delete: bool, environments: tuple[str, ...] = ()
) -> bool:
    """Delete the app's OpenBao entries if nothing blocks them; with the switch off, only say so.

    True once they're gone. Charon can't see whether an entry exists, so that means: deleted in this pass - a delete
    answers the same for one already gone, so every pass after the first repeats it harmlessly.
    """
    if found := blocked:
        log.info("%s secrets: kept: %s", name, "; ".join(found))
        return False
    if delete:
        changes = [  # D123's change entries, the app's and each environment's
            f"{ns}/eso/{n}" for ns in (name, *environments) for n in vault.names(ns) if n.startswith("env-")
        ]
        for entry in secret_entries(name, environments) + changes:
            vault.delete(entry)
        log.info("%s secrets: deleted", name)
        return True
    log.info("%s secrets: would delete (dry run)", name)
    return False


def purge_images(name: str, blocked: list[str], registry: Registry, delete: bool) -> bool:
    """Delete the app's manifests from Zot if nothing blocks them; with the switch off, only say so. True once none."""
    digests = registry.manifests(name)
    if not digests:
        log.info("%s images: nothing left", name)
        return True
    if blocked:
        log.info("%s images: kept %d: %s", name, len(digests), "; ".join(blocked))
        return False
    if delete:
        for digest in digests:
            registry.delete(name, digest)
        log.info("%s images: deleted %d", name, len(digests))
        return True
    log.info("%s images: would delete %d (dry run)", name, len(digests))
    return False


def purge_app(app: AppRequest, outside: Outside, seen: Seen, switches: Switches, now: datetime) -> bool:
    """Take one removed app through every step, each behind its own checks and switch. True once nothing is left."""
    name = app.metadata.name
    removed_at = app.metadata.removed_at
    if removed_at is None:
        log.warning("%s: kept: removed, but the request has no removedAt", name)
        return False
    owned = app.namespaces  # the app's, its build's and each environment's (D126)
    environments = owned[2:]
    namespaces = [seen.namespaces[n] for n in owned if n in seen.namespaces]
    volumes = [v for v in seen.volumes if v.waiting and v.claim_namespace in owned]
    cluster = outside.cluster
    argo_apps = [a for a in app.applications if cluster.has_application(a)]
    common = app_blockers(app, removed_at, argo_apps, now)
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
    secrets_gone = purge_secrets(name, blocked, outside.vault, switches.secrets, environments)
    images_gone = purge_images(name, blocked, outside.registry, switches.images)
    if blocked:
        return False
    repo_open = outside.repos.not_archived(name)
    if repo_open:
        log.info("%s repo: not archived, needs Mario's click", name)
    # A volume released in this pass is still listed: the provisioner removes it after, so the next pass sees it gone.
    volumes_left = any(v.claim_namespace in owned for v in seen.volumes)
    return secrets_gone and images_gone and not repo_open and not volumes_left


def propose_removal(app: AppRequest, path: str, text: str, removals: Removals, propose: bool) -> None:
    """Step 5: ask for the request file to go - once, behind its own switch."""
    name = app.metadata.name
    if removals.pending(name):
        log.info("%s request: removal PR already open", name)
    elif propose:
        removals.propose(name, path, text)
        log.info("%s request: removal PR opened", name)
    else:
        log.info("%s request: would propose deleting %s (dry run)", name, path)


def purge(files: RequestFiles, outside: Outside, switches: Switches, now: datetime) -> int:
    """One pass over every app request. One app failing doesn't stop the others. Returns how many failed."""
    seen = Seen(namespaces=outside.cluster.namespaces(), volumes=outside.cluster.volumes())
    failed = 0
    for path in files.paths():
        try:
            text = files.raw(path)
            app = parse(text)
            if app.spec.removed and purge_app(app, outside, seen, switches, now):
                propose_removal(app, path, text, outside.removals, switches.removal)
        except READ_ERRORS:
            log.exception("%s: failed", path)
            failed += 1
    return failed
