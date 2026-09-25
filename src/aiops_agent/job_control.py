"""Stop one Azure Container Apps Job execution without granting job-secret access."""

from __future__ import annotations

import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from azure.core.credentials import TokenCredential


class JobStopError(RuntimeError):
    """The job execution could not be stopped promptly."""


class JobStopper:
    def __init__(
        self, credential: TokenCredential, subscription_id: str, resource_group: str, job_name: str
    ) -> None:
        self._credential = credential
        self._subscription_id = str(UUID(subscription_id))
        if not re.fullmatch(r"[A-Za-z0-9_.()-]{1,90}", resource_group):
            raise ValueError("AZURE_RESOURCE_GROUP is invalid")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}[a-z0-9]", job_name):
            raise ValueError("WORKER_JOB_NAME is invalid")
        self._resource_group = resource_group
        self._job_name = job_name

    def stop_execution(self, execution_name: str) -> None:
        if not re.fullmatch(rf"{re.escape(self._job_name)}-[a-z0-9]+", execution_name):
            raise JobStopError("invalid worker execution name")
        token = self._credential.get_token("https://management.azure.com/.default").token
        url = (
            f"https://management.azure.com/subscriptions/{self._subscription_id}"
            f"/resourceGroups/{self._resource_group}/providers/Microsoft.App/jobs/{self._job_name}"
            f"/executions/{execution_name}/stop?api-version=2025-07-01"
        )
        request = Request(url, data=b"", headers={"Authorization": f"Bearer {token}"}, method="POST")
        try:
            with urlopen(request, timeout=10) as response:
                if response.status not in {200, 202}:
                    raise JobStopError(f"Azure rejected the stop request (HTTP {response.status})")
        except HTTPError as exc:
            if exc.code not in {404, 409}:
                raise JobStopError(f"Azure rejected the stop request (HTTP {exc.code})") from exc
        except (URLError, TimeoutError) as exc:
            raise JobStopError("could not contact Azure to stop the worker") from exc
