import sqlite3
from dataclasses import dataclass
from fractions import Fraction

from archive.evidence import trigger_slots_digest, trigger_window_digest
from archive.readback import ArchiveVerificationError
from archive.schema import decode_natural, encode_natural


@dataclass(frozen=True, slots=True)
class TriggerWindowRecord:
    session: str
    trigger_slot: int
    first_slot: int
    last_slot: int
    frame_bytes: int
    slots: tuple[tuple[int, bytes | None], ...]
    gray_sum: int
    pixel_count: int


def insert_trigger_window(connection: sqlite3.Connection, record: TriggerWindowRecord) -> None:
    slots_digest = trigger_slots_digest(record.slots)
    digest = trigger_window_digest(
        record.session, record.trigger_slot, record.first_slot, record.last_slot,
        record.frame_bytes, record.gray_sum, record.pixel_count, slots_digest,
    )
    connection.execute(
        '''INSERT INTO trigger_windows (
               session, trigger_slot, first_slot, last_slot, frame_bytes,
               gray_sum, pixel_count, slots_digest, window_digest
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            record.session, encode_natural(record.trigger_slot),
            encode_natural(record.first_slot), encode_natural(record.last_slot),
            encode_natural(record.frame_bytes), encode_natural(record.gray_sum),
            encode_natural(record.pixel_count), slots_digest, digest,
        ),
    )
    connection.executemany(
        '''INSERT INTO trigger_slots (session, trigger_slot, ordinal, slot, payload)
           VALUES (?, ?, ?, ?, ?)''',
        (
            (
                record.session, encode_natural(record.trigger_slot), ordinal,
                encode_natural(slot), payload,
            )
            for ordinal, (slot, payload) in enumerate(record.slots)
        ),
    )


def _decode_window_row(row: sqlite3.Row) -> dict[str, int | str]:
    header: dict[str, int | str] = {
        'trigger_slot': decode_natural(row['trigger_slot']),
        'first_slot': decode_natural(row['first_slot']),
        'last_slot': decode_natural(row['last_slot']),
        'frame_bytes': decode_natural(row['frame_bytes']),
        'gray_sum': decode_natural(row['gray_sum']),
        'pixel_count': decode_natural(row['pixel_count']),
        'slots_digest': row['slots_digest'],
        'window_digest': row['window_digest'],
    }
    if not header['first_slot'] <= header['trigger_slot'] <= header['last_slot']:
        raise ValueError('fixed trigger slot interval is invalid')
    if header['frame_bytes'] <= 0:
        raise ValueError('fixed frame byte count must be positive')
    return header


def read_trigger_window(
    connection: sqlite3.Connection, session: str, trigger_slot: int
) -> dict[str, object] | None:
    row = connection.execute(
        'SELECT * FROM trigger_windows WHERE session = ? AND trigger_slot = ?',
        (session, encode_natural(trigger_slot)),
    ).fetchone()
    if row is None:
        return None
    try:
        header = _decode_window_row(row)
        first_slot = header['first_slot']
        last_slot = header['last_slot']
        rows = connection.execute(
            '''SELECT ordinal, slot, payload FROM trigger_slots
               WHERE session = ? AND trigger_slot = ? ORDER BY ordinal''',
            (session, encode_natural(trigger_slot)),
        ).fetchall()
        if len(rows) != last_slot - first_slot + 1:
            raise ValueError('stored slot rows do not cover the fixed interval')
        slots = []
        gray_sum = 0
        pixel_count = 0
        for ordinal, slot_row in enumerate(rows):
            if type(slot_row['ordinal']) is not int or slot_row['ordinal'] != ordinal:
                raise ValueError(f'slot ordinal {ordinal} is missing')
            n = decode_natural(slot_row['slot'])
            if n != first_slot + ordinal:
                raise ValueError('stored slots are not the dense fixed interval')
            payload = slot_row['payload']
            if payload is not None:
                if not isinstance(payload, bytes) or len(payload) != header['frame_bytes']:
                    raise ValueError('stored payload disagrees with the fixed frame shape')
                gray_sum += sum(payload)
                pixel_count += len(payload)
            slots.append((n, payload))
        if (gray_sum, pixel_count) != (header['gray_sum'], header['pixel_count']):
            raise ValueError('stored totals disagree with the stored frames')
        if trigger_slots_digest(tuple(slots)) != header['slots_digest']:
            raise ValueError('trigger slots digest mismatch')
        expected_digest = trigger_window_digest(
            session, header['trigger_slot'], first_slot, last_slot, header['frame_bytes'],
            header['gray_sum'], header['pixel_count'], header['slots_digest'],
        )
        if expected_digest != header['window_digest']:
            raise ValueError('trigger window digest mismatch')
    except (ValueError, TypeError) as exc:
        raise ArchiveVerificationError(f'trigger window {session!r}/{trigger_slot}: {exc}') from exc
    result_slots = tuple(
        (n, None, None) if payload is None else (n, (session, n), payload)
        for n, payload in slots
    )
    return {
        'session': session,
        'trigger_slot': header['trigger_slot'],
        'first_slot': first_slot,
        'last_slot': last_slot,
        'slots': result_slots,
        'gray_sum': gray_sum,
        'pixel_count': pixel_count,
        'mean': Fraction(gray_sum, pixel_count) if pixel_count > 0 else None,
    }
