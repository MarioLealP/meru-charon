"""Purge step 1 (D109): a retired app's namespaces. Two keys, set by different hands: the request says when it was
removed and how long to keep it, and Shikigami's retire pass marked the namespace with that same moment. A namespace
goes only when every check passes, so a restore, a second retire or a half-done retire keeps it."""

import logging
from datetime import UTC, datetime

import httpx2
import yaml
from pydantic import ValidationError

from charon.cluster import Cluster, Namespace
from charon.request_files import AppRequest, RequestFiles

log = logging.getLogger("charon")

READ_ERRORS = (httpx2.HTTPError, ValidationError, yaml.YAMLError)


def stamp(moment: datetime) -> str:
    """The mark's format: UTC, to the second, as Shikigami writes it from removedAt."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def blockers(app: AppRequest, removed_at: datetime, namespace: Namespace, argo_app: bool, now: datetime) -> list[str]:
    """Everything that keeps this namespace. All of them, not the first: a dry run shows what is still in the way."""
    found = []
    until = removed_at + app.retention
    if now < until:
        found.append(f"retention runs to {stamp(until)}")
    if namespace.removed_at is None:
        found.append("no removed-at mark")
    elif namespace.removed_at != stamp(removed_at):
        found.append(f"mark {namespace.removed_at} isn't the request's {stamp(removed_at)}")
    if argo_app:
        found.append("its Argo Application still exists")
    return found


def purge_app(app: AppRequest, cluster: Cluster, namespaces: dict[str, Namespace], delete: bool, now: datetime) -> None:
    name = app.metadata.name
    removed_at = app.metadata.removedAt
    if removed_at is None:
        log.warning("%s: kept: removed, but the request has no removedAt", name)
        return
    present = [namespaces[n] for n in (name, f"{name}-build") if n in namespaces]
    if not present:
        log.info("%s: no namespace left", name)
        return
    argo_app = cluster.has_application(name)
    for namespace in present:
        target = namespace.metadata.name
        if found := blockers(app, removed_at, namespace, argo_app, now):
            log.info("%s: kept: %s", target, "; ".join(found))
        elif delete:
            cluster.delete(namespace)
            log.info("%s: deleted", target)
        else:
            log.info("%s: would delete (dry run)", target)


def purge(files: RequestFiles, cluster: Cluster, delete: bool, now: datetime) -> int:
    """One pass over every app request. One app failing doesn't stop the others. Returns how many failed."""
    namespaces = cluster.namespaces()
    failed = 0
    for path in files.paths():
        try:
            app = files.read(path)
            if app.spec.removed:
                purge_app(app, cluster, namespaces, delete, now)
        except READ_ERRORS:
            log.exception("%s: failed", path)
            failed += 1
    return failed
