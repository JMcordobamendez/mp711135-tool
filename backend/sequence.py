"""Runs voltage ramps / step sequences on the device from the backend, so a
sequence keeps going even if the browser tab that started it is closed."""

import asyncio

from .device import DeviceError, MP711135
from .models import SequenceRequest, SequenceStatus

# Voltage update period while ramping. The device resolves 10 mV, so finer
# steps than this buy nothing and just load the serial link.
RAMP_TICK_S = 0.1
# How often a hold re-checks for a protection trip.
HOLD_TICK_S = 0.2


class SequenceAborted(Exception):
    pass


class SequenceRunner:
    def __init__(self, call, device: MP711135, fault_active):
        """call: coroutine that runs a device method under the device lock.
        fault_active: callable returning a fault name ("OVP"...) or None."""
        self._call = call
        self._device = device
        self._fault_active = fault_active
        self._task: asyncio.Task | None = None
        self.status = SequenceStatus()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, req: SequenceRequest) -> SequenceStatus:
        await self.stop()
        self.status = SequenceStatus(
            running=True, step=0, cycle=0, total_steps=len(req.steps), repeat=req.repeat
        )
        self._task = asyncio.create_task(self._run(req))
        return self.status

    async def stop(self) -> SequenceStatus:
        if self.running:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self.status.running = False
        return self.status

    def _check_fault(self) -> None:
        fault = self._fault_active()
        if fault:
            raise SequenceAborted(f"{fault} protection tripped")

    async def _sleep_until(self, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        while (remaining := deadline - loop.time()) > 0:
            self._check_fault()
            await asyncio.sleep(min(HOLD_TICK_S, remaining))
        self._check_fault()

    async def _run(self, req: SequenceRequest) -> None:
        dev = self._device
        try:
            prev_v = await self._call(dev.get_voltage)
            for cycle in range(req.repeat):
                for i, step in enumerate(req.steps):
                    self.status.cycle, self.status.step = cycle, i
                    self._check_fault()
                    # Output is switched at the start of the step: turning it
                    # on before a ramp gives a soft start, and turning it off
                    # first is the safe order.
                    if step.output is not None:
                        await self._call(dev.set_output, step.output)
                    if step.current is not None:
                        await self._call(dev.set_current, step.current)
                    # Timed against the clock, not by summing sleeps, so the
                    # serial round-trips don't stretch a ramp or a hold.
                    loop = asyncio.get_running_loop()
                    t0 = loop.time()
                    if step.ramp_s > 0:
                        n = max(1, round(step.ramp_s / RAMP_TICK_S))
                        for k in range(1, n + 1):
                            v = prev_v + (step.voltage - prev_v) * k / n
                            await self._call(dev.set_voltage, round(v, 2))
                            await self._sleep_until(t0 + step.ramp_s * k / n)
                    else:
                        await self._call(dev.set_voltage, step.voltage)
                    prev_v = step.voltage
                    await self._sleep_until(loop.time() + step.hold_s)
        except SequenceAborted as exc:
            self.status.error = str(exc)
        except DeviceError as exc:
            self.status.error = f"device error: {exc}"
        finally:
            self.status.running = False
