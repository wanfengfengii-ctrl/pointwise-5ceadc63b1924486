import argparse
import json
import tempfile
import unittest
from fractions import Fraction
from os import PathLike
from pathlib import Path

from archive.profile_store import ProfileStore
from archive.schema import open_archive_connection
from archive.window_store import WindowStore
from camera.profiles import ProfileRegistry
from camera.sessions import SessionRegistry
from inspection.event_capture import validate_capture_config
from review.handover import HandoverService
from samples.late_trigger import (
    CONFIG as LATE_CONFIG, EVENTS as LATE_EVENTS, EXPECTED_PIXELS, EXPECTED_TOTALS,
    EXPECTED_VISIBLE, SinglePassEvents,
)
from source.events import CaptureConfig, CaptureEvent, Frame, SampleFrontier, Trigger, TriggerFrontier
from source.replay import PacketCapture, ReplaySource

ROOT = Path(__file__).resolve().parent


class Station:
    def __init__(
        self, database: str | PathLike[str], capture_config: CaptureConfig | None = None
    ) -> None:
        if capture_config is not None:
            validate_capture_config(capture_config)
        self.connection = open_archive_connection(database)
        self.profiles = ProfileRegistry(ProfileStore(self.connection))
        self.sessions = SessionRegistry(self.connection, self.profiles)
        self.windows = WindowStore(self.connection)
        self.capture = PacketCapture(self.sessions, self.windows, capture_config)
        self.review = HandoverService(self.windows)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> 'Station':
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _verify() -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def _scenario(
    case_id: str, inputs: dict, expected: object, actual: object,
    downstream_expected: object, downstream_actual: object, code_paths: list[str], oracle: str,
) -> None:
    if actual != expected or downstream_actual != downstream_expected:
        raise AssertionError(f'{case_id}: {(actual, downstream_actual)!r} disagrees with the sample')
    record = {
        'case_id': case_id, 'input': inputs, 'expected': expected, 'actual': actual,
        'downstream_expected': downstream_expected, 'downstream_actual': downstream_actual,
        'code_paths': code_paths, 'oracle': oracle,
    }
    print('POINTWISE_SCENARIO ' + json.dumps(record, ensure_ascii=False, sort_keys=True))


def _demo() -> None:
    with tempfile.TemporaryDirectory(prefix='strip-station-') as directory:
        _demo_dense_packets(Path(directory))
        _demo_late_trigger(Path(directory))


def _demo_dense_packets(directory: Path) -> None:
    database = directory / 'handover.sqlite3'
    received = []
    with Station(database) as station:
        profile = station.profiles.register_profile('C1', 'R1', 1, 1, 20)
        station.sessions.open_session('A', profile)
        with ReplaySource((ROOT / 'samples' / 'day-shift.jsonl').open(encoding='utf-8')) as source:
            while (packet := source.next_packet()) is not None:
                received.append((packet, station.capture.capture_packet('A', packet)))
        station.profiles.register_profile('C1', 'R2', 1, 1, 5)

    with Station(database) as station:
        (light_packet, light), (dark_packet, dark) = received
        fixed = station.windows.read_window('A', light.capture_id)
        dark_fixed = station.windows.read_window('A', dark.capture_id)
        if fixed is None or dark_fixed is None:
            raise RuntimeError('published sample window missing after reopening')
        entries = station.review.handover('A', dark_only=True)
        light_totals = [light.totals.gray_sum, light.totals.pixel_count, light.grade.value]
        light_frames = [[f.slot, list(f.payload), f.origin.position] for f in fixed.frames]
        _scenario(
            'exact_luminance_case',
            {'threshold': fixed.summary.profile.threshold,
             'frames': [list(f.payload) for f in light_packet.frames]},
            [60, 3, 'pass'], light_totals,
            [[4, [10], 101], [5, [20], 108], [6, [30], 121]], light_frames,
            ['inspection/admission.py', 'inspection/luminance.py', 'archive/readback.py'],
            '10 + 20 + 30 = 60, with three pixels and 60 equal to 20 × 3',
        )
        report = [{
            'capture_id': entry.capture_id, 'packet_id': entry.packet_id,
            'threshold': entry.profile.threshold, 'grade': entry.grade.value,
            'totals': [entry.totals.gray_sum, entry.totals.pixel_count],
            'origins': [[slot, origin.camera, origin.device, origin.position]
                        for slot, origin in entry.origins],
        } for entry in entries]
        _scenario(
            'reopen_case',
            {'session': 'A', 'dark_only': True,
             'candidate_frames': [[list(f.payload) for f in p.frames] for p, _ in received]},
            {'totals': [20, 2], 'payloads': [[0], [20]], 'grade': 'dark'},
            {'totals': [dark_fixed.summary.totals.gray_sum, dark_fixed.summary.totals.pixel_count],
             'payloads': [list(f.payload) for f in dark_fixed.frames],
             'grade': dark_fixed.summary.grade.value},
            [{'capture_id': 2, 'packet_id': 'dark-B', 'threshold': 20, 'grade': 'dark',
              'totals': [20, 2], 'origins': [[9, 'C1', 'head-left', 201],
                                             [10, 'C1', 'head-left', 202]]}],
            report,
            ['archive/readback.py', 'review/selection.py', 'review/verify.py', 'review/handover.py'],
            '0 and 20 are both valid pixels, so their mean is 10 under the fixed threshold 20',
        )
        if fixed.packet != light_packet or dark_fixed.packet != dark_packet:
            raise AssertionError('reopened packets disagree with camera input')


def _mean_json(mean: Fraction | None) -> object:
    if mean is None:
        return None
    if mean.denominator == 1:
        return mean.numerator
    return f'{mean.numerator}/{mean.denominator}'


def _event_json(event: CaptureEvent) -> list:
    if isinstance(event, Frame):
        return ['frame', event.n, list(event.payload)]
    if isinstance(event, Trigger):
        return ['trigger', event.t]
    if isinstance(event, SampleFrontier):
        return ['sample-frontier', event.s]
    if isinstance(event, TriggerFrontier):
        return ['trigger-frontier', event.w]
    raise ValueError(f'unsupported capture event {event!r}')


def _trigger_window_json(window: dict) -> dict:
    return {
        'trigger_slot': window['trigger_slot'],
        'window': [window['first_slot'], window['last_slot']],
        'slots': [
            [n, list(origin) if origin is not None else None,
             list(payload) if payload is not None else None]
            for n, origin, payload in window['slots']
        ],
        'gray_sum': window['gray_sum'],
        'pixel_count': window['pixel_count'],
        'mean': _mean_json(window['mean']),
    }


def _demo_late_trigger(directory: Path) -> None:
    database = directory / 'late-trigger.sqlite3'
    observed = {}
    with Station(database, capture_config=LATE_CONFIG) as station:
        profile = station.profiles.register_profile('C1', 'R1', 1, 1, 20)
        station.sessions.open_session('A', profile)

        def observe(step: int) -> None:
            observed[step] = tuple(
                t for t in EXPECTED_PIXELS if station.windows.window('A', t) is not None
            )

        station.capture.run('A', SinglePassEvents(LATE_EVENTS, observe))

    checkpoints = sorted(EXPECTED_VISIBLE)
    expected_visible = {}
    for step in range(1, len(LATE_EVENTS) + 1):
        previous = [k for k in checkpoints if k <= step]
        expected_visible[step] = EXPECTED_VISIBLE[previous[-1]] if previous else ()

    with Station(database) as station:
        actual_windows = {}
        for t in EXPECTED_PIXELS:
            window = station.windows.window('A', t)
            if window is None:
                raise RuntimeError('published trigger window missing after reopening')
            actual_windows[t] = _trigger_window_json(window)

    expected_windows = {}
    for t, pixels in EXPECTED_PIXELS.items():
        first_slot = max(0, t - LATE_CONFIG.P)
        gray_sum, pixel_count = EXPECTED_TOTALS[t]
        expected_windows[t] = {
            'trigger_slot': t,
            'window': [first_slot, t + LATE_CONFIG.Q],
            'slots': [
                [first_slot + i, ['A', first_slot + i], [value]] if value is not None
                else [first_slot + i, None, None]
                for i, value in enumerate(pixels)
            ],
            'gray_sum': gray_sum,
            'pixel_count': pixel_count,
            'mean': _mean_json(Fraction(gray_sum, pixel_count)),
        }
    _scenario(
        'late_trigger_overlap_case',
        {'session': 'A', 'config': [LATE_CONFIG.P, LATE_CONFIG.Q, LATE_CONFIG.D, LATE_CONFIG.L],
         'events': [_event_json(event) for event in LATE_EVENTS]},
        {'visible': expected_visible}, {'visible': observed},
        {'windows': expected_windows}, {'windows': actual_windows},
        ['inspection/event_capture.py', 'archive/trigger_store.py', 'archive/window_store.py'],
        'trigger 1 publishes [0,2] once S reaches 2 and trigger 2 publishes [0,3] once S '
        'reaches 3; slot 1 is confirmed missing and both windows cite the same original '
        'frame for slot 0',
    )


def main() -> int:
    parser = argparse.ArgumentParser(description='Offline strip inspection handover')
    parser.add_argument('action', choices=('verify', 'demo'))
    args = parser.parse_args()
    if args.action == 'verify':
        return _verify()
    _demo()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
