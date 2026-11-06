"""charon: Meru's purge (D83, D105, D107).

Once a day, as a CronJob: an app removed more than its retention ago (`metadata.removedAt` in Meru/requests) loses what
the retire only revoked. One pass, then exit - 0 when every step it reached is done or kept, 1 otherwise, so the
CronJob's last successful run is what Zabbix watches.

Each purge step comes with its own credential, one at a time. So far: the namespaces, the volumes, the OpenBao
entries, the images (purge.py) and, last, the request file itself (removal.py). Then the sweep of change entries
nothing points at any more (sweep.py, D124), which reports to Zabbix through a ConfigMap.
"""

import logging
import ssl
import sys
from datetime import UTC, datetime

import httpx2
import sentry_sdk

from charon.clients.cluster import Cluster
from charon.clients.registry import Registry
from charon.clients.repos import AppRepos
from charon.clients.request_files import RequestFiles
from charon.clients.vault import Vault
from charon.config import Settings
from charon.purge.purge import READ_ERRORS, Outside, Switches, purge
from charon.purge.removal import Removals
from charon.sweep.sweep import SweepReport, sweep


def sweep_and_report(files: RequestFiles, outside: Outside, kubernetes: httpx2.Client, settings: Settings) -> int:
    """The sweep, then its report. A sweep that can't finish writes no report: Zabbix's stale check catches it."""
    now = datetime.now(UTC)
    try:
        report, failed = sweep(files, outside, settings.sweep_entries, now)
        SweepReport(http=kubernetes, namespace=settings.report_namespace, name=settings.report_name).write(report, now)
    except READ_ERRORS:
        logging.getLogger("charon").exception("sweep: failed")
        return 1
    return failed


def run() -> int:
    """One pass. Returns the exit code."""
    settings = Settings()
    kubernetes_token = (settings.kubernetes_dir / "token").read_text().strip()
    with (
        httpx2.Client(
            base_url=settings.gitea_url,
            headers={"Authorization": f"token {settings.gitea_token.get_secret_value()}"},
        ) as gitea,
        httpx2.Client(
            base_url=settings.kubernetes_url,
            headers={"Authorization": f"Bearer {kubernetes_token}"},
            verify=ssl.create_default_context(cafile=settings.kubernetes_dir / "ca.crt"),
        ) as kubernetes,
        httpx2.Client(
            base_url=settings.openbao_addr,
            verify=ssl.create_default_context(cafile=settings.openbao_dir / "ca.crt"),
        ) as openbao,
        httpx2.Client(base_url=settings.zot_url) as zot,
        httpx2.Client(base_url=settings.gitea_url) as anonymous_gitea,
    ):
        vault = Vault.login(openbao, settings.openbao_role, (settings.openbao_dir / "token").read_text().strip())
        files = RequestFiles(http=gitea, repo=settings.requests_repo)
        outside = Outside(
            cluster=Cluster(http=kubernetes),
            vault=vault,
            registry=Registry(http=zot, user=settings.zot_user, password=settings.zot_password),
            repos=AppRepos(http=anonymous_gitea, org=settings.apps_org),
            removals=Removals(
                http=gitea,
                gitea_url=settings.gitea_url,
                repo=settings.requests_repo,
                user=settings.gitea_user,
                token=settings.gitea_token,
            ),
        )
        failed = purge(
            files,
            outside,
            Switches(
                namespaces=settings.delete_namespaces,
                volumes=settings.delete_volumes,
                secrets=settings.delete_secrets,
                images=settings.delete_images,
                removal=settings.propose_removal,
            ),
            datetime.now(UTC),
        )
        failed += sweep_and_report(files, outside, kubernetes, settings)
    logging.getLogger("charon").info("pass done: %d failed", failed)
    return 1 if failed else 0


def main() -> None:
    """Set up logging, run one pass and exit with its code."""
    # Crash reports to GlitchTip (D125): SENTRY_DSN comes from nuwa-app's glitchtip.dsn; unset, the SDK stays off.
    # No local variables or request bodies in a report - either can hold a secret.
    sentry_sdk.init(include_local_variables=False, max_request_body_size="never")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    sys.exit(run())


if __name__ == "__main__":
    main()
