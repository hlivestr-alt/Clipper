from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from clipper_app.contracts.models import PipelineRunCommand, QueueRunCommand, QueueSupervisorCommand


@dataclass(frozen=True)
class LegacyPipelineAdapter:
    executor: Callable[..., dict[str, Any]]

    def __call__(self, *, command: PipelineRunCommand, runtime_cfg: Any, progress_callback) -> dict[str, Any]:
        return self.executor(
            command=command,
            runtime_cfg=runtime_cfg,
            progress_callback=progress_callback,
        )


@dataclass(frozen=True)
class LegacyQueueRunnerAdapter:
    factory: Callable[[QueueRunCommand], Any]

    def __call__(self, command: QueueRunCommand) -> Any:
        return self.factory(command)


@dataclass(frozen=True)
class LegacyQueueSupervisorAdapter:
    executor: Callable[[QueueSupervisorCommand], int]

    def __call__(self, command: QueueSupervisorCommand) -> int:
        return int(self.executor(command))
