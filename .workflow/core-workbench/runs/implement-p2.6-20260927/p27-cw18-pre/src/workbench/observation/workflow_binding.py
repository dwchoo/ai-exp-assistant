"""Bind CW-11 exit observation to one persisted CW-10 WorkflowRun identity."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID, uuid5

from workbench.observation.worker_review import ActiveRunRef, ExitSourceToken, WorkerReviewScheduler
from workbench.workflow.run import WorkflowRun


@dataclass(frozen=True, slots=True)
class WorkflowObservationBinding:
    workflow_run: WorkflowRun
    run: ActiveRunRef
    _source: ExitSourceToken | None = field(default=None, init=False, repr=False, compare=False)
    _scheduler: WorkerReviewScheduler | None = field(default=None, init=False, repr=False, compare=False)

    @classmethod
    def resolve_run(cls, workflow_run: WorkflowRun) -> ActiveRunRef:
        """Read only the persisted TASK identity; this does not issue authority."""
        if not isinstance(workflow_run, WorkflowRun):
            raise TypeError("WorkflowRun required")
        message = workflow_run.repository.get_message(workflow_run.task_message_id)
        content = message.get("content")
        if not isinstance(content, Mapping):
            raise ValueError("persisted TASK content is missing")
        if (message.get("message_id") != workflow_run.task_message_id
                or message.get("task_id") != workflow_run.task_id
                or type(message.get("revision")) is not int
                or message["revision"] != workflow_run.revision
                or message.get("run_id") != workflow_run.run_id
                or content.get("task_id") != workflow_run.task_id
                or type(content.get("revision")) is not int
                or content["revision"] != workflow_run.revision
                or content.get("run_id") != workflow_run.run_id
                or content.get("sender_role") != "manager"
                or content.get("target_role") != "worker"
                or content.get("kind") != "task"):
            raise ValueError("persisted TASK identity or direction mismatch")
        try:
            revision_id = str(uuid5(UUID(workflow_run.task_id),
                                    f"task-spec-revision:{workflow_run.revision}"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Task identity is invalid") from exc
        if content.get("revision_id") != revision_id:
            raise ValueError("persisted TASK revision identity mismatch")
        return ActiveRunRef(
            workflow_run.task_id, revision_id, workflow_run.revision,
            workflow_run.run_id, content.get("target_session_id"),
            content.get("target_session_generation"),
        )

    @classmethod
    def attach(cls, workflow_run: WorkflowRun) -> WorkflowObservationBinding:
        return cls(workflow_run, cls.resolve_run(workflow_run))

    def bind(self, scheduler: WorkerReviewScheduler, source: ExitSourceToken) -> bool:
        """Accept only the token issued for this WorkflowRun at run activation."""
        if not isinstance(scheduler, WorkerReviewScheduler):
            raise TypeError("WorkerReviewScheduler required")
        if self._source is not None:
            return (self._scheduler is scheduler and self._source is source
                    and scheduler.admission.external_source_current(
                        source, self.run, self.workflow_run))
        if not scheduler.admission.external_source_current(source, self.run, self.workflow_run):
            return False
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_scheduler", scheduler)
        return True

    def collect_and_notify(self, scheduler: WorkerReviewScheduler, *, timeout: float = 5,
                           paused: bool = False) -> tuple[dict[str, Any], bool]:
        if not isinstance(scheduler, WorkerReviewScheduler):
            raise TypeError("WorkerReviewScheduler required")
        if self._source is None or self._scheduler is not scheduler:
            raise RuntimeError("WorkflowObservationBinding must bind to this scheduler before collection")
        workflow_run = self.workflow_run
        record = workflow_run.collect(timeout=timeout, paused=paused)
        if not isinstance(record, dict):
            return {}, False
        if (record.get("task_id") != self.run.task_id
                or type(record.get("revision")) is not int
                or record["revision"] != self.run.revision
                or record.get("run_id") != self.run.run_id
                or record != workflow_run._record
                or workflow_run._terminal is not True
                or "ended" not in workflow_run._observed
                or record.get("shell_state") != "exited"
                or record.get("exit_confirmed") is not True
                or (record.get("exit_status") is not None
                    and type(record["exit_status"]) is not int)):
            return record, False
        lifecycle = workflow_run.shell.snapshot().get("lifecycle")
        if (not isinstance(lifecycle, Mapping)
                or lifecycle.get("control_returned") is not True
                or lifecycle.get("input_returned") is not True
                or lifecycle.get("lifetime") != "ended"):
            return record, False
        event = {
            "task_id": self.run.task_id, "revision_id": self.run.revision_id,
            "revision": self.run.revision, "run_id": self.run.run_id,
            "session_id": self.run.session_id,
            "session_generation": self.run.session_generation,
            "exit_confirmed": True, "exit_status": record["exit_status"],
        }
        return record, scheduler.notify_exit_event(event, source=self._source, run=self.run)
