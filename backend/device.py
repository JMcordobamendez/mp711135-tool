from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

import serial

# Empirically required: the device needs a brief moment to process a write-only
# command before it can respond to the next query. Without this, an immediate
# write-then-query (e.g. "set voltage" followed by "read back voltage") times
# out reliably (confirmed 15/15 failures with zero delay, 0/20 failures at
# 20ms). 30ms gives headroom while staying imperceptible for user-driven
# actions.
_WRITE_SETTLE_SECONDS = 0.03


class DeviceError(Exception):
    pass


class DeviceTimeoutError(DeviceError):
    pass


class RegulationMode(str, Enum):
    STANDBY = "STANDBY"
    CV = "CV"
    CC = "CC"
    FAULT = "FAULT"


_MODE_BY_CODE = {
    0: RegulationMode.STANDBY,
    1: RegulationMode.CV,
    2: RegulationMode.CC,
    3: RegulationMode.FAULT,
}


@dataclass
class Measurement:
    voltage: float
    current: float
    power: float
    ovp_fault: bool
    ocp_fault: bool
    otp_fault: bool
    mode: RegulationMode


class MP711135:
    def __init__(self, port: str, baud: int, timeout: float):
        self._port = port
        self._baud = baud
        self._timeout = timeout
        self._serial: serial.Serial | None = None

    def open(self) -> None:
        try:
            self._serial = serial.Serial(
                self._port, baudrate=self._baud, timeout=self._timeout
            )
            self._serial.reset_input_buffer()
        except serial.SerialException as exc:
            raise DeviceError(f"failed to open {self._port}: {exc}") from exc

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def _write(self, cmd: str) -> None:
        if self._serial is None:
            raise DeviceError("device not open")
        try:
            self._serial.write((cmd + "\n").encode())
        except serial.SerialException as exc:
            raise DeviceError(f"write failed for {cmd!r}: {exc}") from exc
        time.sleep(_WRITE_SETTLE_SECONDS)

    def _query(self, cmd: str) -> str:
        if self._serial is None:
            raise DeviceError("device not open")
        # Drop anything left over from an earlier query (e.g. a reply that
        # arrived after its readline() timed out); otherwise every later query
        # would read the previous command's answer.
        # On POSIX a vanished port makes tcflush raise termios.error, which is
        # neither a SerialException nor an OSError, so catch broadly here.
        try:
            self._serial.reset_input_buffer()
        except Exception as exc:
            raise DeviceError(f"failed to flush input for {cmd!r}: {exc}") from exc
        self._write(cmd)
        try:
            raw = self._serial.readline()
        except serial.SerialException as exc:
            raise DeviceError(f"read failed for {cmd!r}: {exc}") from exc
        if not raw:
            raise DeviceTimeoutError(f"timed out waiting for response to {cmd!r}")
        return raw.decode(errors="replace").strip()

    def idn(self) -> str:
        return self._query("*IDN?")

    def reset(self) -> None:
        self._write("*RST")

    def remote(self) -> None:
        self._write("SYSTem:REMote")

    def local(self) -> None:
        self._write("SYSTem:LOCal")

    def get_output(self) -> bool:
        return self._query("OUTPut?") in ("1", "ON")

    def set_output(self, on: bool) -> None:
        self._write(f"OUTPut {'ON' if on else 'OFF'}")

    def get_voltage(self) -> float:
        return float(self._query("VOLTage?"))

    def set_voltage(self, value: float) -> None:
        self._write(f"VOLTage {value}")

    def get_current(self) -> float:
        return float(self._query("CURRent?"))

    def set_current(self, value: float) -> None:
        self._write(f"CURRent {value}")

    def get_voltage_limit(self) -> float:
        return float(self._query("VOLTage:LIMit?"))

    def set_voltage_limit(self, value: float) -> None:
        self._write(f"VOLTage:LIMit {value}")

    def get_current_limit(self) -> float:
        return float(self._query("CURRent:LIMit?"))

    def set_current_limit(self, value: float) -> None:
        self._write(f"CURRent:LIMit {value}")

    def measure_all_info(self) -> Measurement:
        raw = self._query("MEASure:ALL:INFO?")
        parts = raw.split(",")
        try:
            return Measurement(
                voltage=float(parts[0]),
                current=float(parts[1]),
                power=float(parts[2]),
                ovp_fault=parts[3].strip() == "ON",
                ocp_fault=parts[4].strip() == "ON",
                otp_fault=parts[5].strip() == "ON",
                mode=_MODE_BY_CODE[int(parts[6])],
            )
        except (IndexError, ValueError, KeyError) as exc:
            raise DeviceError(f"malformed MEASure:ALL:INFO? response: {raw!r}") from exc
