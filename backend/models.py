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
