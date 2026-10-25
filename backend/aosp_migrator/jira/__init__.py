import httpx2

from aosp_migrator.config import JIRA_BASE_URL, JIRA_PAT, Config
from aosp_migrator.jira.base import JiraClient, JiraError
from aosp_migrator.jira.datacenter import DataCenterClient


class JiraNotConfiguredError(JiraError):
    pass


def client_from_config(config: Config, transport: httpx2.BaseTransport | None = None) -> JiraClient:
    base_url, pat = config.get(JIRA_BASE_URL), config.get(JIRA_PAT)
    if not base_url or not pat:
        raise JiraNotConfiguredError("Jira base URL and PAT are required")
    return DataCenterClient(base_url, pat, transport=transport)
