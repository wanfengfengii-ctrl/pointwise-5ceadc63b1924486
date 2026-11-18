from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import TYPE_CHECKING

from archive.schema import encode_natural
from source.frame import GrayFrame

if TYPE_CHECKING:
    from camera.profiles import ProfileRevision
    from inspection.grading import WindowGrade
    from inspection.luminance import LuminanceTotals
    from source.packet import PacketHeader


def _encode(fields: tuple) -> bytes:
    return json.dumps(fields, ensure_ascii=True, separators=(',', ':')).encode('ascii')


def frame_evidence_digest(frames: Iterable[GrayFrame]) -> str:
    digest = hashlib.sha256(b'frame-sequence-v1\n')
    for ordinal, frame in enumerate(frames):
        digest.update(_encode((
            ordinal, encode_natural(frame.slot), encode_natural(frame.width),
            encode_natural(frame.height), frame.payload.hex(), frame.origin.camera,
            frame.origin.device, encode_natural(frame.origin.position),
        )))
        digest.update(b'\n')
    return digest.hexdigest()


def window_header_digest(
    session: str, capture_id: int, header: PacketHeader, profile: ProfileRevision,
    totals: LuminanceTotals, grade: WindowGrade, frames_digest: str,
) -> str:
    return hashlib.sha256(_encode((
        'window-header-v1', session, capture_id, header.packet_id, header.camera,
        encode_natural(header.trigger_slot), encode_natural(header.first_slot),
        encode_natural(header.last_slot), profile.key.camera, profile.key.revision,
        encode_natural(profile.spec.width), encode_natural(profile.spec.height),
        profile.threshold, encode_natural(totals.gray_sum), encode_natural(totals.pixel_count),
        grade.value, frames_digest,
    ))).hexdigest()


def trigger_slots_digest(slots: Iterable[tuple[int, bytes | None]]) -> str:
    digest = hashlib.sha256(b'trigger-slots-v1\n')
    for ordinal, (slot, payload) in enumerate(slots):
        digest.update(_encode((
            ordinal, encode_natural(slot), None if payload is None else payload.hex(),
        )))
        digest.update(b'\n')
    return digest.hexdigest()


def trigger_window_digest(
    session: str, trigger_slot: int, first_slot: int, last_slot: int, frame_bytes: int,
    gray_sum: int, pixel_count: int, slots_digest: str,
) -> str:
    return hashlib.sha256(_encode((
        'trigger-window-v1', session, encode_natural(trigger_slot),
        encode_natural(first_slot), encode_natural(last_slot), encode_natural(frame_bytes),
        encode_natural(gray_sum), encode_natural(pixel_count), slots_digest,
    ))).hexdigest()
