"""Charon's settings, from CHARON_* environment variables (Nüwa's env + secretEnv)."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHARON_")

    # Where the work comes from (D83): the request files, read with a token that can only read that repo.
    gitea_url: str = "https://git.clockmost.com"
    requests_repo: str = "Meru/requests"
    gitea_token: SecretStr

    # The cluster, with the pod's own token and CA (nuwa-app kubernetesApi).
    kubernetes_url: str = "https://kubernetes.default.svc"
    kubernetes_dir: Path = Path("/var/run/meru/kubernetes")  # token, ca.crt

    # Off = a pass only says what it would delete. Each stays off until Mario has read a pass's output.
    delete_namespaces: bool = False
    delete_volumes: bool = False
