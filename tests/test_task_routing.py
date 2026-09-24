from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from aiops_agent.harness import build_prompt
from aiops_agent.models import CodingTask, MessageFormatError, RepositoryRecord
from aiops_agent.queue_client import _decode
from aiops_agent.repo import build_branch_name
from aiops_agent.repository_registry import RepositoryNotRegisteredError, RepositoryRegistry


def test_parse_coding_task():
    body = json.dumps(
        {
            "task_id": "240cb99a-0287-4fa1-a296-d976dd0c24bc",
            "repository": "checkout-api",
            "instruction": "Add pagination to the orders endpoint",
            "created_at": "2026-09-23T08:15:00Z",
            "target_branch": "develop",
        }
    )
    task = CodingTask.parse(body)
    assert task.repository == "checkout-api"
    assert task.target_branch == "develop"
    assert task.created_at.tzinfo is not None


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        "[]",
        "{}",
        '{"repository":"repo"}',
        '{"repository":"repo","instruction":"Do it","created_at":"tomorrow"}',
        '{"repository":"repo","instruction":"Do it","merge_when_ready":"yes"}',
        '{"repository":"repo","instruction":"Do it","direct_to_main":"yes"}',
    ],
)
def test_parse_rejects_bad_payloads(body):
    with pytest.raises(MessageFormatError):
        CodingTask.parse(body)


def test_parse_rejects_missing_task_id():
    with pytest.raises(MessageFormatError):
        CodingTask.parse('{"repository":"repo","instruction":"Do the thing"}')


def test_parse_rejects_instruction_that_exceeds_queue_byte_limit():
    with pytest.raises(MessageFormatError, match="UTF-8 bytes"):
        CodingTask.parse(
            json.dumps(
                {
                    "task_id": "240cb99a-0287-4fa1-a296-d976dd0c24bc",
                    "repository": "repo",
                    "instruction": "🛠" * 13_000,
                }
            )
        )


def test_decode_handles_base64_and_plain():
    assert _decode("eyJhIjogMX0=") == '{"a": 1}'
    assert _decode('{"a": 1}') == '{"a": 1}'


def _registry() -> RepositoryRegistry:
    return RepositoryRegistry(
        {
            "checkout-api": RepositoryRecord(
                key="checkout-api",
                name="Checkout API",
                repo_url="https://github.com/contoso/checkout-api.git",
            )
        }
    )


def test_resolve_registered_repository():
    assert _registry().resolve("checkout-api").name == "Checkout API"


def test_repository_can_choose_github_merge_method():
    record = RepositoryRecord.from_dict(
        "api", {"repo_url": "https://github.com/acme/api.git", "github_merge_method": "rebase"}
    )
    assert record.github_merge_method == "REBASE"


def test_resolve_unknown_repository():
    with pytest.raises(RepositoryNotRegisteredError):
        _registry().resolve("unknown")


def test_prompt_contains_task_and_forbids_git_writes():
    task = CodingTask(
        task_id="240cb99a-0287-4fa1-a296-d976dd0c24bc",
        repository="checkout-api",
        instruction="Add pagination",
        created_at=datetime.now(UTC),
    )
    prompt = build_prompt(task, _registry().resolve("checkout-api"))
    assert "Add pagination" in prompt
    assert "Do NOT run git commit" in prompt


def test_prompt_includes_only_this_sessions_previous_messages():
    task = CodingTask(
        task_id="240cb99a-0287-4fa1-a296-d976dd0c24bc",
        repository="checkout-api",
        instruction="Now add tests",
        created_at=datetime.now(UTC),
    )
    prompt = build_prompt(
        task,
        _registry().resolve("checkout-api"),
        [
            {"role": "user", "content": "Add pagination"},
            {"role": "assistant", "content": "Pagination implemented"},
        ],
    )
    assert "user: Add pagination" in prompt
    assert "assistant: Pagination implemented" in prompt
    assert "## Latest user message\nNow add tests" in prompt


def test_branch_name_is_stable_for_task():
    assert build_branch_name("agent/task", "Checkout API", "240cb99a-0287-4fa1-a296") == (
        "agent/task/checkout-api-240cb99a-028"
    )
