"""Disposable Container App Job entrypoint.

KEDA starts one replica per queued task; this process drains the messages it
can see, then exits with a status code the platform can act on.
"""

from __future__ import annotations

import os
import sys
from typing import NoReturn

from azure.identity import DefaultAzureCredential

from .config import ConfigError, Settings
from .logging_setup import configure_logging, get_logger
from .models import CodingTask, MessageFormatError
from .pipeline import CodingPipeline
from .queue_client import DequeuedMessage, queue_reader
from .repository_registry import RepositoryNotRegisteredError
from .session import SessionConflictError, SessionStore, TurnAlreadyCompleted
from .status import TaskStatusStore

log = get_logger(__name__)


def main() -> int:
    configure_logging()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        log.error("configuration_invalid", error=str(exc))
        return 2

    credential = DefaultAzureCredential()
    failures = 0
    handled = 0

    try:
        pipeline = CodingPipeline(settings, credential)
        status_store = TaskStatusStore(settings.status_container_url, credential)
        session_store = SessionStore(settings.status_container_url, credential)
    except Exception as exc:  # startup failures are not message-specific
        log.exception("startup_failed", error=str(exc))
        if "pipeline" in locals():
            pipeline.close()
        if "status_store" in locals():
            status_store.close()
        if "session_store" in locals():
            session_store.close()
        credential.close()
        return 2

    try:
        with queue_reader(settings, credential) as reader:
            for message in reader.receive():
                handled += 1
                if not _process(message, pipeline, status_store, settings.queue_max_dequeue_count, session_store):
                    failures += 1
    except Exception as exc:
        log.exception("job_failed", error=str(exc))
        return 1 if handled else 2
    finally:
        pipeline.close()
        status_store.close()
        session_store.close()
        credential.close()

    log.info("job_complete", handled=handled, failures=failures)
    if handled == 0:
        log.info("queue_empty")
    return 1 if failures else 0


def _process(
    message: DequeuedMessage,
    pipeline: CodingPipeline,
    status_store: TaskStatusStore,
    max_dequeue_count: int,
    session_store: SessionStore | None = None,
) -> bool:
    try:
        task = CodingTask.parse(message.body)
    except MessageFormatError as exc:
        log.error("message_unparseable", error=str(exc))
        message.complete()  # poison message: never retryable
        return False

    log.info("task_received", task_id=task.task_id, repository=task.repository, attempt=message.dequeue_count)
    try:
        if task.session_id and session_store is None:
            raise RuntimeError("session task received without a session store")
        execution_name = os.environ.get("CONTAINER_APP_JOB_EXECUTION_NAME")
        history = (
            session_store.start_turn(task, execution_name)
            if task.session_id and session_store else []
        )
        status_store.write(
            task.task_id,
            repository=task.repository,
            owner_id=task.owner_id,
            state="running",
            attempt=message.dequeue_count,
            execution_name=execution_name,
        )
        result = (
            pipeline.handle(task, history, before_push=lambda: session_store.ensure_turn_active(task))
            if task.session_id and session_store else pipeline.handle(task)
        )
        state = "no_changes" if not result.files_changed and not result.pushed else "completed"
        if task.session_id and session_store:
            session_store.complete_turn(task, result)
        status_store.write(
            task.task_id,
            repository=task.repository,
            owner_id=task.owner_id,
            state=state,
            branch=result.branch if result.pushed else None,
            files_changed=result.files_changed,
            summary=result.harness_summary[:6000],
            pull_request_url=result.pull_request_url,
            auto_merge_enabled=result.auto_merge_enabled,
        )
    except TurnAlreadyCompleted:
        log.info("turn_already_completed", task_id=task.task_id, session_id=task.session_id)
        message.complete()
        return True
    except SessionConflictError as exc:
        log.error("session_turn_conflict", task_id=task.task_id, session_id=task.session_id, error=str(exc))
        message.complete()
        return False
    except RepositoryNotRegisteredError as exc:
        log.error("repository_unregistered", task_id=task.task_id, repository=task.repository)
        try:
            status_store.write(
                task.task_id,
                repository=task.repository,
                owner_id=task.owner_id,
                state="failed",
                error_type=type(exc).__name__,
            )
        except Exception:
            log.exception("task_status_write_failed", task_id=task.task_id)
        if task.session_id and session_store:
            try:
                session_store.fail_turn(task, "This repository is no longer available.")
            except Exception:
                log.exception("session_update_failed", task_id=task.task_id)
        message.complete()
        return False
    except Exception as exc:
        give_up = message.dequeue_count >= max_dequeue_count
        log.exception(
            "task_failed",
            task_id=task.task_id,
            repository=task.repository,
            error=str(exc),
            attempt=message.dequeue_count,
            give_up=give_up,
        )
        try:
            status_store.write(
                task.task_id,
                repository=task.repository,
                owner_id=task.owner_id,
                state="failed" if give_up else "retrying",
                attempt=message.dequeue_count,
                error_type=type(exc).__name__,
                pull_request_url=getattr(exc, "pull_request_url", None),
            )
        except Exception:
            log.exception("task_status_write_failed", task_id=task.task_id)
        if task.session_id and session_store:
            try:
                if give_up:
                    session_store.fail_turn(task)
                else:
                    session_store.mark_retrying(task)
            except Exception:
                log.exception("session_update_failed", task_id=task.task_id)
        if give_up:
            message.complete()
        else:
            message.abandon()
        return False

    message.complete()
    log.info(
        "task_handled",
        task_id=task.task_id,
        repository=task.repository,
        branch=result.branch,
        pushed=result.pushed,
        files_changed=len(result.files_changed),
    )
    return True


def _shutdown(code: int) -> NoReturn:
    """Hard-exit: SDK background threads or stray child processes must not keep the replica alive."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def run() -> NoReturn:
    _shutdown(main())


if __name__ == "__main__":
    run()
