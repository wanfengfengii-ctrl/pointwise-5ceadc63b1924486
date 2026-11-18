from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    P: int
    Q: int
    D: int
    L: int


@dataclass(frozen=True, slots=True)
class Frame:
    n: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class Trigger:
    t: int


@dataclass(frozen=True, slots=True)
class SampleFrontier:
    s: int


@dataclass(frozen=True, slots=True)
class TriggerFrontier:
    w: int


CaptureEvent = Frame | Trigger | SampleFrontier | TriggerFrontier
