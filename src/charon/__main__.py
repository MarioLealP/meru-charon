"""charon: Meru's purge (D83, D105, D107). Once a day, as a CronJob: an app removed more than its retention ago
(`metadata.removedAt` in Meru/requests) loses what the retire only revoked. One pass, then exit - 0 when every step
it reached is done or kept, 1 otherwise, so the CronJob's last successful run is what Zabbix watches.

Each purge step comes with its own credential, one at a time. So far: the namespaces (purge.py)."""

import logging
import ssl
import sys
from datetime import UTC, datetime

import httpx2

from charon.cluster import Cluster
from charon.config import Settings
from charon.purge import purge
from charon.request_files import RequestFiles


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
    ):
        failed = purge(
            RequestFiles(gitea, settings.requests_repo),
            Cluster(kubernetes),
            settings.delete_namespaces,
            datetime.now(UTC),
        )
    logging.getLogger("charon").info("pass done: %d failed", failed)
    return 1 if failed else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    sys.exit(run())


if __name__ == "__main__":
    main()
