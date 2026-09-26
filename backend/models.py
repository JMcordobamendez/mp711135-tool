# Optional[...] rather than `X | None`: pydantic evaluates these at runtime and
# the Pi may run Python 3.9 (Raspberry Pi OS Bullseye).
from typing import List, Optional

from pydantic import BaseModel, Field


class OutputSetRequest(BaseModel):
    on: bool


class VoltageSetRequest(BaseModel):
    value: float = Field(ge=0, le=60)


class CurrentSetRequest(BaseModel):
    value: float = Field(ge=0, le=10)


# Manual documents OVP 0-61V / OCP 0-10.1A, but the real device ships with
# factory-default limits of 62.0V / 10.2A (confirmed live) — outside those
# documented ranges. Bounds here match confirmed hardware behavior, not the
# (apparently slightly conservative) manual figures.
class VoltageLimitSetRequest(BaseModel):
    value: float = Field(ge=0, le=62)


class CurrentLimitSetRequest(BaseModel):
    value: float = Field(ge=0, le=10.2)


class MeasurementResponse(BaseModel):
    voltage: float
    current: float
    power: float
    ovp_fault: bool
    ocp_fault: bool
    otp_fault: bool
    mode: str


class OutputResponse(BaseModel):
    output: bool


class VoltageResponse(BaseModel):
    voltage_setpoint: float


class CurrentResponse(BaseModel):
    current_setpoint: float


class VoltageLimitResponse(BaseModel):
    voltage_limit: float


class CurrentLimitResponse(BaseModel):
    current_limit: float


class IdnResponse(BaseModel):
    raw: str


class StateResponse(BaseModel):
    output: bool
    voltage_setpoint: float
    current_setpoint: float
    voltage_limit: float
    current_limit: float
    measurement: MeasurementResponse


class SetpointsResponse(BaseModel):
    output: bool
    voltage_setpoint: float
    current_setpoint: float
    voltage_limit: float
    current_limit: float


class SequenceStep(BaseModel):
    voltage: float = Field(ge=0, le=60)
    # None leaves the current limit / output state as they are.
    current: Optional[float] = Field(default=None, ge=0, le=10)
    output: Optional[bool] = None
    # Seconds to ramp linearly from the previous voltage to `voltage`.
    ramp_s: float = Field(default=0, ge=0, le=3600)
    # Seconds to stay at `voltage` once reached.
    hold_s: float = Field(default=0, ge=0, le=86400)


class SequenceRequest(BaseModel):
    steps: List[SequenceStep] = Field(min_length=1, max_length=100)
    repeat: int = Field(default=1, ge=1, le=10000)


class SequenceStatus(BaseModel):
    running: bool = False
    step: Optional[int] = None  # 0-based index of the step being executed
    cycle: Optional[int] = None  # 0-based repetition
    total_steps: int = 0
    repeat: int = 0
    error: Optional[str] = None
