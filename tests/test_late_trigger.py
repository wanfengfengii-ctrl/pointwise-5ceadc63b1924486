import sqlite3
from fractions import Fraction

from archive.schema import _SCHEMA_V1
from inspection.event_capture import EventRound
from samples.late_trigger import (
    CONFIG, EVENTS, EXPECTED_PIXELS, EXPECTED_TOTALS, EXPECTED_VISIBLE,
    SinglePassEvents, background_events,
)
from source.events import CaptureConfig, Frame, SampleFrontier, Trigger, TriggerFrontier
from source.replay import PacketCapture
from station import Station
from support import StationCase, packet


class LateTriggerTests(StationCase):
    def configured_station(self, config=CONFIG):
        station = Station(self.database, capture_config=config)
        self.addCleanup(station.close)
        return station

    def bind_on(
        self, station, session='A', camera='C1', revision='R1',
        width=1, height=1, threshold=20,
    ):
        key = station.profiles.register_profile(camera, revision, width, height, threshold)
        return station.sessions.open_session(session, key)

    def assert_sample_window(self, station, t):
        window = station.windows.window('A', t)
        self.assertIsNotNone(window)
        first = max(0, t - CONFIG.P)
        expected_slots = tuple(
            (first + i, ('A', first + i), bytes([value])) if value is not None
            else (first + i, None, None)
            for i, value in enumerate(EXPECTED_PIXELS[t])
        )
        gray_sum, pixel_count = EXPECTED_TOTALS[t]
        self.assertEqual(window['session'], 'A')
        self.assertEqual(window['trigger_slot'], t)
        self.assertEqual((window['first_slot'], window['last_slot']), (first, t + CONFIG.Q))
        self.assertEqual(window['slots'], expected_slots)
        self.assertEqual((window['gray_sum'], window['pixel_count']), (gray_sum, pixel_count))
        self.assertEqual(window['mean'], Fraction(gray_sum, pixel_count))
        self.assertIsInstance(window['mean'], Fraction)
        return window

    def test_sample_replay_and_reopen(self):
        station = self.configured_station()
        self.bind_on(station)
        visible = {}

        def observe(step):
            visible[step] = tuple(
                t for t in EXPECTED_PIXELS if station.windows.window('A', t) is not None
            )

        source = SinglePassEvents(EVENTS, observe)
        self.assertIsNone(station.capture.run('A', source))
        self.assertEqual(source.read_count, len(EVENTS))
        for step, expected in EXPECTED_VISIBLE.items():
            self.assertEqual(visible[step], expected, f'visible set after event {step}')
        expected_full = {}
        checkpoints = sorted(EXPECTED_VISIBLE)
        for step in range(1, len(EVENTS) + 1):
            previous = [k for k in checkpoints if k <= step]
            expected_full[step] = EXPECTED_VISIBLE[previous[-1]] if previous else ()
        self.assertEqual(visible, expected_full)

        first = self.assert_sample_window(station, 1)
        second = self.assert_sample_window(station, 2)
        self.assertEqual(first['slots'][0], (0, ('A', 0), bytes([10])))
        self.assertEqual(second['slots'][0], (0, ('A', 0), bytes([10])))
        self.assertIsNone(station.windows.window('A', 3))
        self.assertIsNone(station.windows.window('B', 1))
        for bad in (-1, True, '1', 1.0):
            self.assertIsNone(station.windows.window('A', bad))

        with self.open_station() as other:
            self.assert_sample_window(other, 1)
            self.assert_sample_window(other, 2)
        station.close()

        with self.open_station() as reopened:
            first = self.assert_sample_window(reopened, 1)
            second = self.assert_sample_window(reopened, 2)
            self.assertEqual(first['slots'][0], (0, ('A', 0), bytes([10])))
            self.assertEqual(second['slots'][0], (0, ('A', 0), bytes([10])))

    def test_out_of_order_window_statistics(self):
        station = self.configured_station(CaptureConfig(P=2, Q=2, D=4, L=6))
        self.bind_on(station, session='B')
        station.capture.run('B', [
            Frame(3, bytes([30])), Frame(1, bytes([10])), Frame(0, bytes([5])),
            Frame(2, bytes([20])), SampleFrontier(3), Trigger(2), SampleFrontier(4),
        ])
        window = station.windows.window('B', 2)
        self.assertEqual(window['slots'], (
            (0, ('B', 0), bytes([5])), (1, ('B', 1), bytes([10])),
            (2, ('B', 2), bytes([20])), (3, ('B', 3), bytes([30])), (4, None, None),
        ))
        self.assertEqual((window['gray_sum'], window['pixel_count']), (65, 4))
        self.assertEqual(window['mean'], Fraction(65, 4))
        self.assertIsNone(station.windows.window('B', 0))
        self.assertIsNone(station.windows.window('B', 3))

    def test_multi_pixel_statistics_count_zero_pixels(self):
        station = self.configured_station(CaptureConfig(P=1, Q=1, D=2, L=3))
        self.bind_on(station, session='W', width=2)
        station.capture.run('W', [
            Frame(0, bytes([10, 20])), Frame(1, bytes([0, 5])),
            SampleFrontier(1), Trigger(0), Trigger(1), SampleFrontier(2),
        ])
        first = station.windows.window('W', 0)
        self.assertEqual((first['gray_sum'], first['pixel_count']), (35, 4))
        self.assertEqual(first['mean'], Fraction(35, 4))
        second = station.windows.window('W', 1)
        self.assertEqual((second['gray_sum'], second['pixel_count']), (35, 4))
        self.assertEqual(second['slots'][2], (2, None, None))
        self.assertEqual(second['slots'][0], (0, ('W', 0), bytes([10, 20])))

    def test_all_missing_window(self):
        station = self.configured_station(CaptureConfig(P=2, Q=2, D=2, L=4))
        self.bind_on(station, session='M')
        station.capture.run('M', [SampleFrontier(1), Trigger(0), SampleFrontier(2)])
        window = station.windows.window('M', 0)
        self.assertEqual(window['slots'], ((0, None, None), (1, None, None), (2, None, None)))
        self.assertEqual((window['gray_sum'], window['pixel_count']), (0, 0))
        self.assertIsNone(window['mean'])

    def test_unsealed_window_not_published(self):
        station = self.configured_station(CaptureConfig(P=1, Q=2, D=2, L=4))
        self.bind_on(station, session='U')
        station.capture.run('U', [Frame(0, bytes([9])), SampleFrontier(0), Trigger(0)])
        self.assertIsNone(station.windows.window('U', 0))
        station.close()
        with self.open_station() as reopened:
            self.assertIsNone(reopened.windows.window('U', 0))

    def test_illegal_events_have_no_effect(self):
        config = CaptureConfig(P=1, Q=1, D=2, L=3)
        cases = [
            ('late-frame', Frame(0, bytes([8]))),
            ('far-frame', Frame(4, bytes([8]))),
            ('bool-slot-frame', Frame(True, bytes([8]))),
            ('negative-frame', Frame(-1, bytes([8]))),
            ('long-payload', Frame(2, bytes([8, 8]))),
            ('empty-payload', Frame(2, bytes())),
            ('text-payload', Frame(2, 'x')),
            ('bytearray-payload', Frame(2, bytearray([8]))),
            ('duplicate-trigger', Trigger(0)),
            ('future-trigger', Trigger(2)),
            ('negative-trigger', Trigger(-1)),
            ('bool-trigger', Trigger(True)),
            ('retreating-sample-frontier', SampleFrontier(-1)),
            ('leaping-sample-frontier', SampleFrontier(5)),
            ('passing-trigger-frontier', TriggerFrontier(2)),
            ('missing-event', None),
            ('foreign-event', object()),
        ]
        for index, (name, bad) in enumerate(cases):
            with self.subTest(case=name):
                station = self.configured_station(config)
                session = f'E{index}'
                self.bind_on(station, session=session)
                prefix = [
                    Frame(0, bytes([7])), SampleFrontier(0),
                    Frame(1, bytes([9])), SampleFrontier(1), Trigger(0),
                ]
                with self.assertRaises(ValueError):
                    station.capture.run(session, [*prefix, bad])
                window = station.windows.window(session, 0)
                self.assertIsNotNone(window)
                self.assertEqual((window['gray_sum'], window['pixel_count']), (16, 2))
                windows = station.connection.execute(
                    'SELECT count(*) FROM trigger_windows WHERE session = ?', (session,),
                ).fetchone()[0]
                self.assertEqual(windows, 1)
                slots = station.connection.execute(
                    'SELECT count(*) FROM trigger_slots WHERE session = ?', (session,),
                ).fetchone()[0]
                self.assertEqual(slots, 2)
                station.close()

    def test_duplicate_frame_in_span_rejected(self):
        station = self.configured_station()
        self.bind_on(station, session='D')
        with self.assertRaises(ValueError):
            station.capture.run('D', [Frame(1, bytes([1])), Frame(1, bytes([2]))])

    def test_trigger_frontier_closes_acceptance_without_revoking(self):
        station = self.configured_station(CaptureConfig(P=1, Q=1, D=2, L=3))
        self.bind_on(station, session='T')
        events = [
            Frame(0, bytes([7])), SampleFrontier(0),
            Frame(1, bytes([9])), SampleFrontier(1),
            Trigger(0), TriggerFrontier(1), Trigger(1),
        ]
        with self.assertRaises(ValueError):
            station.capture.run('T', events)
        window = station.windows.window('T', 0)
        self.assertIsNotNone(window)
        self.assertEqual(window['gray_sum'], 16)
        self.assertIsNone(station.windows.window('T', 1))

    def test_frontier_rules(self):
        config = CaptureConfig(P=0, Q=0, D=1, L=2)
        station = self.configured_station(config)
        self.bind_on(station, session='legal')
        station.capture.run('legal', [
            SampleFrontier(-1), TriggerFrontier(-1),
            SampleFrontier(1), TriggerFrontier(1), SampleFrontier(3), TriggerFrontier(2),
        ])
        illegal = [
            ('sample-retreat', [SampleFrontier(0), SampleFrontier(-1)]),
            ('trigger-retreat', [SampleFrontier(1), TriggerFrontier(1), TriggerFrontier(0)]),
            ('trigger-passes-sample', [TriggerFrontier(0)]),
            ('sample-span', [SampleFrontier(2)]),
            ('span-after-trigger', [SampleFrontier(1), TriggerFrontier(0), SampleFrontier(3)]),
            ('bool-sample', [SampleFrontier(True)]),
            ('text-trigger', [TriggerFrontier('1')]),
            ('float-sample', [SampleFrontier(1.0)]),
        ]
        for index, (name, events) in enumerate(illegal):
            with self.subTest(case=name):
                session = f'F{index}'
                self.bind_on(station, session=session)
                with self.assertRaises(ValueError):
                    station.capture.run(session, events)
                count = station.connection.execute(
                    'SELECT count(*) FROM trigger_windows WHERE session = ?', (session,),
                ).fetchone()[0]
                self.assertEqual(count, 0)

    def test_invalid_capture_config_rejected(self):
        bad_configs = [
            CaptureConfig(P=-1, Q=1, D=1, L=1),
            CaptureConfig(P=1, Q=-1, D=1, L=1),
            CaptureConfig(P=1, Q=1, D=0, L=1),
            CaptureConfig(P=1, Q=1, D=2, L=1),
            CaptureConfig(P=True, Q=1, D=1, L=1),
            CaptureConfig(P=1, Q=1.5, D=1, L=1),
            CaptureConfig(P=1, Q=1, D=1, L='4'),
            'not-a-config',
            (2, 1, 3, 4),
        ]
        for config in bad_configs:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    Station(self.database, capture_config=config)
        station = self.open_station()
        self.bind_on(station, session='R')
        capture = PacketCapture(
            station.sessions, station.windows, CaptureConfig(P=1, Q=1, D=0, L=1)
        )
        with self.assertRaises(ValueError):
            capture.run('R', [])

    def test_run_requires_capture_config(self):
        self.bind()
        with self.assertRaises(ValueError):
            self.station.capture.run('A', [])
        receipt = self.station.capture.capture_packet('A', packet('v1', [b'\x14']))
        self.assertEqual(receipt.totals.gray_sum, 20)
        self.assertIsNone(self.station.windows.window('A', 0))

    def test_background_segments_keep_staging_bounded(self):
        config = CaptureConfig(P=2, Q=1, D=3, L=4)
        peaks = {}
        for slot_count in (25, 250):
            engine = EventRound(config, 'A', 1, lambda record: None)
            peak = 0
            for event in background_events(slot_count, 1):
                engine.consume(event)
                peak = max(peak, len(engine.staged_slots))
            peaks[slot_count] = peak
            self.assertEqual(engine.frontiers, (slot_count - 1, slot_count - 1))
        self.assertEqual(peaks[25], peaks[250])
        self.assertEqual(peaks[250], 3)
        self.assertLessEqual(peaks[250], config.P + config.D + config.L)

        station = self.configured_station(config)
        self.bind_on(station)
        self.assertIsNone(station.capture.run('A', background_events(250, 1)))
        for table in ('trigger_windows', 'trigger_slots'):
            count = station.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            self.assertEqual(count, 0)

    def test_staging_release_boundary_with_pending_window(self):
        config = CaptureConfig(P=1, Q=3, D=2, L=6)
        events = [
            Frame(0, bytes([1])), Frame(1, bytes([2])), SampleFrontier(1),
            Frame(2, bytes([3])), Frame(3, bytes([4])), SampleFrontier(3),
            Trigger(1),
            Frame(5, bytes([9])),
            TriggerFrontier(2),
            Frame(4, bytes([5])), SampleFrontier(4),
            SampleFrontier(5), TriggerFrontier(5),
        ]
        published = []
        engine = EventRound(config, 'A', 1, published.append)
        for event in events[:9]:
            engine.consume(event)
        self.assertEqual(engine.staged_slots, (0, 1, 2, 3, 5))
        self.assertEqual(published, [])
        for event in events[9:11]:
            engine.consume(event)
        self.assertEqual(len(published), 1)
        record = published[0]
        self.assertEqual((record.trigger_slot, record.first_slot, record.last_slot), (1, 0, 4))
        self.assertEqual(
            [payload for _, payload in record.slots],
            [bytes([1]), bytes([2]), bytes([3]), bytes([4]), bytes([5])],
        )
        self.assertEqual(engine.staged_slots, (2, 3, 4, 5))
        for event in events[11:]:
            engine.consume(event)
        self.assertEqual(engine.staged_slots, (5,))

        station = self.configured_station(config)
        self.bind_on(station, session='R')
        station.capture.run('R', events)
        window = station.windows.window('R', 1)
        self.assertEqual([slot for slot, _, _ in window['slots']], [0, 1, 2, 3, 4])
        self.assertEqual(window['gray_sum'], 15)
        uncovered = station.connection.execute(
            "SELECT count(*) FROM trigger_slots WHERE slot = '5'"
        ).fetchone()[0]
        self.assertEqual(uncovered, 0)

    def test_second_window_write_failure_keeps_first(self):
        config = CaptureConfig(P=1, Q=1, D=2, L=4)
        station = self.configured_station(config)
        self.bind_on(station)
        station.connection.execute(
            '''CREATE TEMP TRIGGER abort_second_window
               BEFORE INSERT ON trigger_slots WHEN NEW.trigger_slot = '1'
               BEGIN SELECT RAISE(ABORT, 'trigger slot write interrupted'); END'''
        )
        events = [
            Frame(0, bytes([7])), SampleFrontier(0),
            Frame(1, bytes([8])), SampleFrontier(1),
            Trigger(0), Trigger(1),
            Frame(2, bytes([9])), SampleFrontier(2),
        ]
        with self.assertRaises(sqlite3.IntegrityError):
            station.capture.run('A', events)
        self.assertIsNotNone(station.windows.window('A', 0))
        self.assertIsNone(station.windows.window('A', 1))
        stranded = station.connection.execute(
            "SELECT count(*) FROM trigger_windows WHERE trigger_slot = '1'"
        ).fetchone()[0]
        self.assertEqual(stranded, 0)
        station.close()
        with self.open_station() as reopened:
            first = reopened.windows.window('A', 0)
            self.assertIsNotNone(first)
            self.assertEqual(
                first['slots'],
                ((0, ('A', 0), bytes([7])), (1, ('A', 1), bytes([8]))),
            )
            self.assertEqual((first['gray_sum'], first['pixel_count']), (15, 2))
            self.assertEqual(first['mean'], Fraction(15, 2))
            self.assertIsNone(reopened.windows.window('A', 1))

    def test_version1_archive_upgrades(self):
        legacy = self.database.parent / 'legacy.sqlite3'
        connection = sqlite3.connect(legacy)
        try:
            for statement in _SCHEMA_V1:
                connection.execute(statement)
            connection.execute('PRAGMA user_version = 1')
            connection.commit()
        finally:
            connection.close()
        with Station(legacy) as station:
            key = station.profiles.register_profile('C1', 'R1', 1, 1, 20)
            station.sessions.open_session('A', key)
            receipt = station.capture.capture_packet('A', packet('old', [b'\x0a']))
            self.assertEqual(
                station.windows.read_window('A', receipt.capture_id).packet.header.packet_id,
                'old',
            )
        with Station(legacy) as reopened:
            self.assertIsNotNone(reopened.windows.read_window('A', receipt.capture_id))
            self.assertIsNone(reopened.windows.window('A', 0))
