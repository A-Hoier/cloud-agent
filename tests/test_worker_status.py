from __future__ import annotations

import json

from aiops_agent.__main__ import _process
from aiops_agent.models import TaskResult


class FakeMessage:
    body = json.dumps(
        {
            "task_id": "240cb99a-0287-4fa1-a296-d976dd0c24bc",
            "repository": "api",
            "instruction": "Add pagination",
        }
    )
    dequeue_count = 1

    def __init__(self) -> None:
        self.completed = False
        self.abandoned = False

    def complete(self) -> None:
        self.completed = True

    def abandon(self) -> None:
        self.abandoned = True


class FakeStatusStore:
    def __init__(self) -> None:
        self.states = []

    def write(self, task_id, **fields):
        self.states.append(fields["state"])


class SuccessfulPipeline:
    def __init__(self) -> None:
        self.history = None

    def handle(self, task, history=None, before_push=None):
        self.history = history
        if before_push:
            before_push()
        return TaskResult(
            task.task_id,
            "agent/task/api-240cb99a-028",
            True,
            ["api.py"],
            "Implemented pagination",
            pull_request_url="https://github.com/acme/api/pull/12",
        )


class FailingPipeline:
    def handle(self, task, history=None, before_push=None):
        raise RuntimeError("unexpected error")


class FakeSessionStore:
    def __init__(self) -> None:
        self.completed = False
        self.retried = False

    def start_turn(self, task, execution_name=None):
        return [{"role": "user", "content": "Previous request"}]

    def ensure_turn_active(self, task):
        pass

    def complete_turn(self, task, result):
        self.completed = True

    def mark_retrying(self, task):
        self.retried = True


def test_worker_persists_completion_before_deleting_message():
    message = FakeMessage()
    status = FakeStatusStore()
    assert _process(message, SuccessfulPipeline(), status, 3)
    assert status.states == ["running", "completed"]
    assert message.completed


def test_worker_marks_retrying_and_releases_message():
    message = FakeMessage()
    status = FakeStatusStore()
    assert not _process(message, FailingPipeline(), status, 3)
    assert status.states == ["running", "retrying"]
    assert message.abandoned
    assert not message.completed


def test_worker_passes_session_history_and_saves_reply():
    message = FakeMessage()
    message.body = json.dumps(
        {
            "task_id": "240cb99a-0287-4fa1-a296-d976dd0c24bc",
            "session_id": "53c8bc3d-c6e9-4346-8835-367169989aad",
            "repository": "api",
            "instruction": "Add pagination",
        }
    )
    pipeline = SuccessfulPipeline()
    sessions = FakeSessionStore()
    assert _process(message, pipeline, FakeStatusStore(), 3, sessions)
    assert pipeline.history == [{"role": "user", "content": "Previous request"}]
    assert sessions.completed
    assert message.completed
