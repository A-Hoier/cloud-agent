"""Queue a coding task from a local Azure-authenticated shell."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

from azure.identity import DefaultAzureCredential
from azure.storage.queue import QueueClient

from aiops_agent.status import TaskStatusStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", help="repository id from config/repositories.json")
    parser.add_argument("instruction", help="feature or coding task for the agent")
    parser.add_argument("--target-branch")
    parser.add_argument("--merge-when-ready", action="store_true")
    args = parser.parse_args()

    payload = {
        "task_id": str(uuid4()),
        "repository": args.repository,
        "instruction": args.instruction,
        "target_branch": args.target_branch,
        "merge_when_ready": args.merge_when_ready,
        "created_at": datetime.now(UTC).isoformat(),
    }
    credential = DefaultAzureCredential()
    client = QueueClient(
        account_url=os.environ["QUEUE_ACCOUNT_URL"],
        queue_name=os.environ["QUEUE_NAME"],
        credential=credential,
    )
    status_store = TaskStatusStore(os.environ["TASK_STATUS_CONTAINER_URL"], credential)
    try:
        status_store.write(payload["task_id"], repository=payload["repository"], state="queued")
        try:
            client.send_message(json.dumps(payload))
        except Exception:
            status_store.write(payload["task_id"], repository=payload["repository"], state="failed")
            raise
    finally:
        status_store.close()
        client.close()
        credential.close()
    print(payload["task_id"])


if __name__ == "__main__":
    main()
