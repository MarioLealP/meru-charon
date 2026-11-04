"""charon: Meru's purge (D83, D105, D107). Once a day, as a CronJob: an app removed more than its retention ago
(`metadata.removedAt` in Meru/requests) loses what the retire only revoked. One pass, then exit - 0 when every step
it reached is done or kept, 1 otherwise, so the CronJob's last successful run is what Zabbix watches.

Each purge step comes with its own credential, one at a time. So far: the namespaces, the volumes, the OpenBao
entries and the images (purge.py)."""

import logging
import ssl
import sys
from datetime import UTC, datetime

import httpx2

from charon.cluster import Cluster
from charon.config import Settings
from charon.purge import Outside, Switches, purge
from charon.registry import Registry
from charon.request_files import RequestFiles
from charon.vault import Vault


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
    ):
        vault = Vault(openbao, settings.openbao_role, (settings.openbao_dir / "token").read_text().strip())
        failed = purge(
            RequestFiles(gitea, settings.requests_repo),
            Outside(
                cluster=Cluster(kubernetes),
                vault=vault,
                registry=Registry(zot, settings.zot_user, settings.zot_password.get_secret_value()),
            ),
            Switches(
                namespaces=settings.delete_namespaces,
                volumes=settings.delete_volumes,
                secrets=settings.delete_secrets,
                images=settings.delete_images,
            ),
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
