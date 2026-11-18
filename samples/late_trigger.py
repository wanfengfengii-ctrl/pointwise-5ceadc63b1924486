from collections.abc import Callable, Iterable, Iterator

from source.events import CaptureConfig, CaptureEvent, Frame, SampleFrontier, Trigger, TriggerFrontier


CONFIG = CaptureConfig(P=2, Q=1, D=3, L=4)
EVENTS = (
    Frame(2, bytes([30])),
    Frame(0, bytes([10])),
    SampleFrontier(2),
    Trigger(1),
    TriggerFrontier(1),
    Trigger(2),
    TriggerFrontier(2),
    Frame(3, bytes([50])),
    SampleFrontier(3),
)
EXPECTED_VISIBLE = {4: (1,), 7: (1,), 8: (1,), 9: (1, 2)}
EXPECTED_PIXELS = {1: (10, None, 30), 2: (10, None, 30, 50)}
EXPECTED_TOTALS = {1: (40, 2), 2: (90, 3)}


class SinglePassEvents:
    def __init__(
        self, events: Iterable[CaptureEvent], after_event: Callable[[int], None] | None = None,
    ) -> None:
        self._events = events
        self._after_event = after_event
        self._started = False
        self.read_count = 0

    def __iter__(self) -> Iterator[CaptureEvent]:
        if self._started:
            raise AssertionError("event source was opened twice")
        self._started = True
        return self._read()

    def _read(self) -> Iterator[CaptureEvent]:
        for event in self._events:
            self.read_count += 1
            yield event
            if self._after_event is not None:
                self._after_event(self.read_count)


def background_events(slot_count: int, pixel_count: int) -> Iterator[CaptureEvent]:
    for n in range(slot_count):
        yield Frame(n, bytes([n % 256]) * pixel_count)
        yield SampleFrontier(n)
        yield TriggerFrontier(n)
