"""Azure Storage Queue access.

The Container App Job is scaled by the KEDA azure-queue scaler, but KEDA only
starts the replica - the job itself is responsible for dequeuing, handling and
deleting the message.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from azure.core.credentials import TokenCredential
from azure.storage.queue import QueueClient, QueueMessage

from .config import Settings
from .logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class DequeuedMessage:
    body: str
    dequeue_count: int
    _raw: QueueMessage
    _client: QueueClient

    def complete(self) -> None:
        self._client.delete_message(self._raw)

    def abandon(self) -> None:
        """Make the message immediately visible again for another attempt."""
        self._client.update_message(self._raw, visibility_timeout=0)


class QueueReader:
    def __init__(self, settings: Settings, credential: TokenCredential) -> None:
        self._settings = settings
        self._client = QueueClient(
            account_url=settings.queue_account_url,
            queue_name=settings.queue_name,
            credential=credential,
        )

    def receive(self) -> Iterator[DequeuedMessage]:
        messages = self._client.receive_messages(
            max_messages=self._settings.queue_max_messages,
            visibility_timeout=self._settings.queue_visibility_timeout,
        )
        for message in messages:
            yield DequeuedMessage(
                body=_decode(message.content),
                dequeue_count=message.dequeue_count or 1,
                _raw=message,
                _client=self._client,
            )

    def close(self) -> None:
        self._client.close()


@contextmanager
def queue_reader(settings: Settings, credential: TokenCredential) -> Iterator[QueueReader]:
    reader = QueueReader(settings, credential)
    try:
        yield reader
    finally:
        reader.close()


def _decode(content: str | bytes | None) -> str:
    """Storage Queue producers may or may not base64-encode the payload."""
    if content is None:
        return ""
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    try:
        decoded = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        return content
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return content
