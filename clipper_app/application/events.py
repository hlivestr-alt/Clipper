from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Protocol

from clipper_app.contracts.events import ProgressEvent


class EventSink(Protocol):
    def publish(self, event: ProgressEvent) -> None: ...


class NullEventSink:
    def publish(self, event: ProgressEvent) -> None:
        return None


@dataclass
class InMemoryEventSink:
    events: list[ProgressEvent] = field(default_factory=list)

    def publish(self, event: ProgressEvent) -> None:
        self.events.append(event)


class LoggingEventSink:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def publish(self, event: ProgressEvent) -> None:
        self.logger.info(
            event.message,
            extra={
                "operation_id": event.operation_id,
                "operation": event.operation.value,
                "stage": event.stage.value,
                "event_kind": event.kind.value,
                "progress_percent": event.percent,
            },
        )


class LegacyCallbackEventSink:
    def __init__(self, callback: Callable[..., None] | None) -> None:
        self.callback = callback

    def publish(self, event: ProgressEvent) -> None:
        if self.callback is None:
            return
        payload = event.to_legacy_payload()
        if payload:
            try:
                self.callback(event.stage.value, event.percent, event.message, **payload)
                return
            except TypeError:
                pass
        self.callback(event.stage.value, event.percent, event.message)


class CompositeEventSink:
    def __init__(self, *sinks: EventSink) -> None:
        self.sinks = sinks

    def publish(self, event: ProgressEvent) -> None:
        for sink in self.sinks:
            sink.publish(event)


class QueueStateEventSink:
    """Adapter for the queue runner's existing progress callback contract."""

    def __init__(self, callback: Callable[..., None]) -> None:
        self.callback = callback

    def publish(self, event: ProgressEvent) -> None:
        self.callback(
            event.stage.value,
            event.percent,
            event.message,
            **event.to_legacy_payload(),
        )
