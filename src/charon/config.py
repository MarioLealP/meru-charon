"""Charon's settings, from CHARON_* environment variables (Nüwa's env + secretEnv)."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Everything a pass needs from its environment."""

    model_config = SettingsConfigDict(env_prefix="CHARON_", frozen=True)

    # Where the work comes from (D83): the request files. The bot has Read on that repo only; its token is scoped
    # write:repository so it can propose the removal PR (AGit, D119) - a proposal, never a change to main.
    gitea_url: str = "https://git.clockmost.com"
    requests_repo: str = "Meru/requests"
    gitea_token: SecretStr
    gitea_user: str = "charon"  # the bot the token belongs to, for git's login
    apps_org: str = "Meru"

    # The cluster, with the pod's own token and CA (nuwa-app kubernetesApi).
    kubernetes_url: str = "https://kubernetes.default.svc"
    kubernetes_dir: Path = Path("/var/run/meru/kubernetes")  # token, ca.crt

    # OpenBao, with the pod's token for this role and CA (nuwa-app openbaoLogin).
    openbao_addr: str = "https://openbao.clockmost.internal:8204"
    openbao_role: str = "meru-charon-purge"
    openbao_dir: Path = Path("/var/run/meru/openbao")  # token, ca.crt

    # Zot (D112), inside the cluster: reads anonymous, this login only deletes.
    zot_url: str = "http://zot.clockmost.com"
    zot_user: str = "charon"
    zot_password: SecretStr

    # Off = a pass only says what it would delete. Each stays off until Mario has read a pass's output.
    delete_namespaces: bool = False
    delete_volumes: bool = False
    delete_secrets: bool = False
    delete_images: bool = False
    propose_removal: bool = False
    sweep_entries: bool = False  # D124: delete unreferenced change entries (env-*); the count goes to Zabbix either way

    # The sweep's report, which Zabbix reads (Meru/meru rbac/charon-sweep-report.yaml).
    report_namespace: str = "charon"
    report_name: str = "charon-sweep"
