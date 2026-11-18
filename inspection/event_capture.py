from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from archive.trigger_store import TriggerWindowRecord
from source.events import CaptureConfig, CaptureEvent, Frame, SampleFrontier, Trigger, TriggerFrontier

if TYPE_CHECKING:
    from archive.window_store import WindowStore
    from camera.sessions import SessionRegistry


def validate_capture_config(config: CaptureConfig) -> None:
    if not isinstance(config, CaptureConfig):
        raise ValueError('capture config must be a CaptureConfig')
    for name in ('P', 'Q', 'D', 'L'):
        if type(getattr(config, name)) is not int:
            raise ValueError(f'capture config {name} must be an integer')
    if config.P < 0 or config.Q < 0:
        raise ValueError('capture config P and Q must be nonnegative')
    if not 1 <= config.D <= config.L:
        raise ValueError('capture config requires 1 <= D <= L')


def _require_natural(value: int, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f'{name} must be a nonnegative integer')
    return value


class EventRound:
    """One single-pass capture round for one session.

    Tracks the sample frontier S and the trigger frontier W, stages raw frames
    only while a still-acceptable trigger window (including future triggers that
    need S to advance) or an accepted-but-unpublished window can cover them, and
    commits each sealed trigger window atomically through ``publish``. All
    staging is bounded by the fixed config, never by the round length.
    """

    def __init__(
        self,
        config: CaptureConfig,
        session: str,
        pixel_count: int,
        publish: Callable[[TriggerWindowRecord], None],
    ) -> None:
        self._config = config
        self._session = session
        self._pixel_count = pixel_count
        self._publish = publish
        self._sample_frontier = -1
        self._trigger_frontier = -1
        self._frames: dict[int, bytes] = {}
        self._received: set[int] = set()
        self._accepted: set[int] = set()
        self._unpublished: set[int] = set()

    @property
    def staged_slots(self) -> tuple[int, ...]:
        return tuple(sorted(self._frames))

    @property
    def frontiers(self) -> tuple[int, int]:
        return self._sample_frontier, self._trigger_frontier

    def consume(self, event: CaptureEvent) -> None:
        if isinstance(event, Frame):
            self._consume_frame(event)
        elif isinstance(event, Trigger):
            self._consume_trigger(event)
        elif isinstance(event, SampleFrontier):
            self._advance_sample_frontier(event.s)
        elif isinstance(event, TriggerFrontier):
            self._advance_trigger_frontier(event.w)
        else:
            raise ValueError(f'unsupported capture event {event!r}')
        self._commit_ready()
        self._release_staging()

    def _consume_frame(self, event: Frame) -> None:
        n = _require_natural(event.n, 'frame slot')
        payload = event.payload
        if not isinstance(payload, bytes) or len(payload) != self._pixel_count:
            raise ValueError('frame payload must be bytes matching the session pixel count')
        frontier = self._sample_frontier
        if not frontier < n <= frontier + self._config.D:
            raise ValueError(f'frame slot {n} lies outside the acceptance span')
        if n in self._received:
            raise ValueError(f'frame slot {n} was already received this round')
        self._received.add(n)
        self._frames[n] = payload

    def _consume_trigger(self, event: Trigger) -> None:
        t = _require_natural(event.t, 'trigger slot')
        if not self._trigger_frontier < t <= self._sample_frontier:
            raise ValueError(f'trigger slot {t} lies outside the acceptance span')
        if t in self._accepted:
            raise ValueError(f'trigger slot {t} was already accepted this round')
        self._accepted.add(t)
        self._unpublished.add(t)

    def _advance_sample_frontier(self, s: int) -> None:
        if type(s) is not int:
            raise ValueError('sample frontier must be an integer')
        if s < self._sample_frontier:
            raise ValueError('sample frontier cannot retreat')
        if s < self._trigger_frontier:
            raise ValueError('sample frontier cannot precede the trigger frontier')
        if s - self._trigger_frontier > self._config.L:
            raise ValueError('frontier span exceeds the configured limit')
        self._sample_frontier = s
        self._received = {n for n in self._received if n > s}

    def _advance_trigger_frontier(self, w: int) -> None:
        if type(w) is not int:
            raise ValueError('trigger frontier must be an integer')
        if w < self._trigger_frontier:
            raise ValueError('trigger frontier cannot retreat')
        if w > self._sample_frontier:
            raise ValueError('trigger frontier cannot pass the sample frontier')
        if self._sample_frontier - w > self._config.L:
            raise ValueError('frontier span exceeds the configured limit')
        self._trigger_frontier = w
        self._accepted = {t for t in self._accepted if t > w}

    def _commit_ready(self) -> None:
        frontier = self._sample_frontier
        ready = sorted(t for t in self._unpublished if frontier >= t + self._config.Q)
        for t in ready:
            self._commit_window(t)
            self._unpublished.discard(t)

    def _commit_window(self, t: int) -> None:
        config = self._config
        first = max(0, t - config.P)
        last = t + config.Q
        slots = []
        gray_sum = 0
        pixel_count = 0
        for n in range(first, last + 1):
            payload = self._frames.get(n)
            slots.append((n, payload))
            if payload is not None:
                gray_sum += sum(payload)
                pixel_count += len(payload)
        self._publish(TriggerWindowRecord(
            self._session, t, first, last, self._pixel_count,
            tuple(slots), gray_sum, pixel_count,
        ))

    def _release_staging(self) -> None:
        keep_from = self._trigger_frontier - self._config.P + 1
        pending = tuple(
            (max(0, t - self._config.P), t + self._config.Q) for t in self._unpublished
        )
        for n in tuple(self._frames):
            if n >= keep_from:
                continue
            if any(first <= n <= last for first, last in pending):
                continue
            del self._frames[n]


def run_event_capture(
    sessions: SessionRegistry,
    windows: WindowStore,
    config: CaptureConfig | None,
    session: str,
    events: Iterable[CaptureEvent],
) -> None:
    if config is None:
        raise ValueError('event capture requires a capture_config')
    validate_capture_config(config)
    binding = sessions.get_session(session)
    capture = EventRound(
        config, binding.session, binding.profile.spec.pixel_count,
        windows.publish_trigger_window,
    )
    for event in events:
        capture.consume(event)
