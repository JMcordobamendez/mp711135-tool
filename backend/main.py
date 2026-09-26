import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import config as cfg
from .device import DeviceError, MP711135, Measurement
from .sequence import SequenceRunner
from .models import (
    CurrentLimitResponse,
    CurrentLimitSetRequest,
    CurrentResponse,
    CurrentSetRequest,
    IdnResponse,
    MeasurementResponse,
    OutputResponse,
    OutputSetRequest,
    SequenceRequest,
    SequenceStatus,
    SetpointsResponse,
    StateResponse,
    VoltageLimitResponse,
    VoltageLimitSetRequest,
    VoltageResponse,
    VoltageSetRequest,
)

logger = logging.getLogger("uvicorn.error")

# Setpoints aren't part of MEASure:ALL:INFO?, so they're re-read at this rate
# to pick up changes made on the supply's own front panel.
SETPOINT_SYNC_S = 1.0


async def call(app: FastAPI, fn, *args, **kwargs):
    async with app.state.device_lock:
        return await asyncio.to_thread(fn, *args, **kwargs)


def _measurement_response(m: Measurement) -> MeasurementResponse:
    return MeasurementResponse(
        voltage=m.voltage,
        current=m.current,
        power=m.power,
        ovp_fault=m.ovp_fault,
        ocp_fault=m.ocp_fault,
        otp_fault=m.otp_fault,
        mode=m.mode.value,
    )


def _read_setpoints(device: MP711135) -> SetpointsResponse:
    return SetpointsResponse(
        output=device.get_output(),
        voltage_setpoint=device.get_voltage(),
        current_setpoint=device.get_current(),
        voltage_limit=device.get_voltage_limit(),
        current_limit=device.get_current_limit(),
    )


def _active_fault(app: FastAPI) -> str | None:
    m = app.state.last_measurement
    if m is None:
        return None
    return "OVP" if m.ovp_fault else "OCP" if m.ocp_fault else "OTP" if m.otp_fault else None


async def _broadcast(app: FastAPI, payload: dict) -> None:
    stale = []
    for ws in app.state.ws_clients:
        try:
            await ws.send_json(payload)
        except Exception:
            stale.append(ws)
    for ws in stale:
        app.state.ws_clients.discard(ws)


async def _reconnect_device(app: FastAPI) -> bool:
    device = app.state.device
    async with app.state.device_lock:
        try:
            await asyncio.to_thread(device.close)
            await asyncio.to_thread(device.open)
            await asyncio.to_thread(device.remote)
        except DeviceError:
            return False
    return True


async def poll_loop(app: FastAPI) -> None:
    interval = 1 / cfg.POLL_HZ
    loop = asyncio.get_running_loop()
    next_setpoint_sync = 0.0
    while True:
        await asyncio.sleep(interval)
        sequence = app.state.sequence.status.model_dump()
        if not app.state.device_connected:
            if not await _reconnect_device(app):
                await _broadcast(app, {"device_connected": False, "sequence": sequence})
                continue
            app.state.device_connected = True
            next_setpoint_sync = 0.0
        try:
            measurement = await call(app, app.state.device.measure_all_info)
            setpoints = None
            if loop.time() >= next_setpoint_sync:
                setpoints = await call(app, _read_setpoints, app.state.device)
                next_setpoint_sync = loop.time() + SETPOINT_SYNC_S
        except DeviceError:
            app.state.device_connected = False
            app.state.last_measurement = None
            await _broadcast(app, {"device_connected": False, "sequence": sequence})
            continue
        app.state.last_measurement = measurement
        payload = _measurement_response(measurement).model_dump()
        payload["device_connected"] = True
        payload["sequence"] = sequence
        if setpoints is not None:
            payload["setpoints"] = setpoints.model_dump()
        await _broadcast(app, payload)


@asynccontextmanager
async def lifespan(app: FastAPI):
    device = MP711135(port=cfg.PORT, baud=cfg.BAUD, timeout=cfg.TIMEOUT)
    # Don't crash if the supply is off or unplugged at boot: start
    # disconnected and let poll_loop keep retrying until it shows up.
    try:
        device.open()
        device.remote()
        connected = True
    except DeviceError as exc:
        logger.warning("MP711135 not available at startup, will keep retrying: %s", exc)
        device.close()
        connected = False
    app.state.device = device
    app.state.device_lock = asyncio.Lock()
    app.state.device_connected = connected
    app.state.last_measurement = None
    app.state.ws_clients = set()
    app.state.sequence = SequenceRunner(
        lambda fn, *a: call(app, fn, *a), device, lambda: _active_fault(app)
    )
    poll_task = asyncio.create_task(poll_loop(app))
    yield
    await app.state.sequence.stop()
    poll_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await poll_task
    with contextlib.suppress(DeviceError):
        device.local()
    device.close()


app = FastAPI(lifespan=lifespan)

# The frontend is a static file served from a different origin/port than this
# API (e.g. opened directly from disk or via a dev static server), so the
# browser enforces CORS on every fetch() call. This is a single-user bench
# tool, not a multi-tenant service, so allowing any origin is acceptable.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(DeviceError)
async def device_error_handler(request, exc: DeviceError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/idn", response_model=IdnResponse)
async def get_idn():
    raw = await call(app, app.state.device.idn)
    return IdnResponse(raw=raw)


@app.get("/measurements", response_model=MeasurementResponse)
async def get_measurements():
    m = await call(app, app.state.device.measure_all_info)
    return _measurement_response(m)


@app.get("/state", response_model=StateResponse)
async def get_state():
    device = app.state.device
    async with app.state.device_lock:
        setpoints = await asyncio.to_thread(_read_setpoints, device)
        measurement = await asyncio.to_thread(device.measure_all_info)
    return StateResponse(
        **setpoints.model_dump(),
        measurement=_measurement_response(measurement),
    )


@app.put("/output", response_model=OutputResponse)
async def set_output(body: OutputSetRequest):
    await call(app, app.state.device.set_output, body.on)
    value = await call(app, app.state.device.get_output)
    return OutputResponse(output=value)


@app.put("/voltage", response_model=VoltageResponse)
async def set_voltage(body: VoltageSetRequest):
    await call(app, app.state.device.set_voltage, body.value)
    value = await call(app, app.state.device.get_voltage)
    return VoltageResponse(voltage_setpoint=value)


@app.put("/current", response_model=CurrentResponse)
async def set_current(body: CurrentSetRequest):
    await call(app, app.state.device.set_current, body.value)
    value = await call(app, app.state.device.get_current)
    return CurrentResponse(current_setpoint=value)


@app.put("/voltage-limit", response_model=VoltageLimitResponse)
async def set_voltage_limit(body: VoltageLimitSetRequest):
    await call(app, app.state.device.set_voltage_limit, body.value)
    value = await call(app, app.state.device.get_voltage_limit)
    return VoltageLimitResponse(voltage_limit=value)


@app.put("/current-limit", response_model=CurrentLimitResponse)
async def set_current_limit(body: CurrentLimitSetRequest):
    await call(app, app.state.device.set_current_limit, body.value)
    value = await call(app, app.state.device.get_current_limit)
    return CurrentLimitResponse(current_limit=value)


@app.post("/faults/reset", response_model=MeasurementResponse)
async def reset_faults():
    # No documented SCPI "clear fault" command exists, so this forces OUTPut
    # OFF and re-reads status. Confirmed against real hardware: deliberately
    # tripping OVP (voltage-limit set below the voltage setpoint with output
    # on) puts the device in FAULT mode with ovp_fault=true, and OUTPut OFF
    # does clear it (mode returns to STANDBY, ovp_fault=false).
    await call(app, app.state.device.set_output, False)
    m = await call(app, app.state.device.measure_all_info)
    return _measurement_response(m)


@app.websocket("/ws/measurements")
async def ws_measurements(websocket: WebSocket):
    await websocket.accept()
    app.state.ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        app.state.ws_clients.discard(websocket)


@app.get("/sequence", response_model=SequenceStatus)
async def get_sequence():
    return app.state.sequence.status


@app.post("/sequence", response_model=SequenceStatus)
async def start_sequence(body: SequenceRequest):
    """Start a sequence of steps (replacing any running one). Each step may
    switch the output and set the current limit, then ramps (or jumps) to its
    voltage and holds it. Aborts if a protection trips or the device fails."""
    if not app.state.device_connected:
        raise DeviceError("device not connected")
    return await app.state.sequence.start(body)


@app.delete("/sequence", response_model=SequenceStatus)
async def stop_sequence():
    """Stop the running sequence, leaving the output as it is."""
    return await app.state.sequence.stop()
