"""A simulated MP711135 on a pseudo-terminal, for tests and for trying the
UI without hardware.

    python -m tests.fake_device /tmp/mp711135
    MP711135_PORT=/tmp/mp711135 .venv/bin/uvicorn backend.main:app

It answers the SCPI subset the backend uses and models a resistive load
(`load_ohms`), so CV/CC mode and OVP/OCP trips behave like the real thing.
"""

import os
import pty
import sys
import threading
import tty


class FakeMP711135:
    def __init__(self, link_path: str, load_ohms: float = 10.0):
        self.link_path = link_path
        self.load_ohms = load_ohms
        self.output = False
        self.voltage = 0.0
        self.current = 1.0
        self.voltage_limit = 62.0
        self.current_limit = 10.2
        self.fault: str | None = None  # "OVP" / "OCP"
        self.commands: list[str] = []  # every command received, in order
        self._lock = threading.Lock()
        self._master = None
        self._slave = None
        self._thread = None
        self._stop = threading.Event()

    # ---- lifecycle ----
    def start(self) -> "FakeMP711135":
        self._master, self._slave = pty.openpty()
        tty.setraw(self._slave)
        try:
            os.unlink(self.link_path)
        except FileNotFoundError:
            pass
        os.symlink(os.ttyname(self._slave), self.link_path)
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Simulates unplugging the USB cable."""
        self._stop.set()
        try:
            os.unlink(self.link_path)
        except FileNotFoundError:
            pass
        for fd in (self._master, self._slave):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._master = self._slave = None
        if self._thread:
            self._thread.join(timeout=2)

    def inject(self, raw: str) -> None:
        """Writes unsolicited bytes, e.g. a late reply to an earlier query."""
        os.write(self._master, raw.encode())

    # ---- model ----
    def _measure(self):
        if not self.output or self.fault:
            return 0.0, 0.0, "3" if self.fault else "0"
        i_load = self.voltage / self.load_ohms
        if i_load > self.current:
            return self.current * self.load_ohms, self.current, "2"  # CC
        return self.voltage, i_load, "1"  # CV

    def _check_protection(self):
        if self.output and not self.fault:
            v, i, _ = self._measure()
            if self.voltage > self.voltage_limit:
                self.fault = "OVP"
            elif i > self.current_limit:
                self.fault = "OCP"

    def handle(self, cmd: str) -> str | None:
        with self._lock:
            self.commands.append(cmd)
            u = cmd.strip().upper()
            head, _, arg = u.partition(" ")
            if u == "*IDN?":
                return "multicomp pro,MP711135,FAKE,FV:V2.0.0"
            if u in ("SYSTEM:REMOTE", "SYSTEM:LOCAL", "SYST:REM", "SYST:LOC"):
                return None
            if head == "OUTPUT":
                self.output = arg == "ON"
                if not self.output:
                    self.fault = None  # OUTPut OFF clears a latched fault
            elif u == "OUTPUT?":
                return "ON" if self.output else "OFF"
            elif u == "VOLTAGE?":
                return f"{self.voltage:.2f}"
            elif u == "CURRENT?":
                return f"{self.current:.3f}"
            elif u == "VOLTAGE:LIMIT?":
                return f"{self.voltage_limit:.2f}"
            elif u == "CURRENT:LIMIT?":
                return f"{self.current_limit:.3f}"
            elif head == "VOLTAGE:LIMIT":
                self.voltage_limit = float(arg)
            elif head == "CURRENT:LIMIT":
                self.current_limit = float(arg)
            elif head == "VOLTAGE":
                self.voltage = float(arg)
            elif head == "CURRENT":
                self.current = float(arg)
            elif u == "MEASURE:ALL:INFO?":
                self._check_protection()
                v, i, mode = self._measure()
                flag = lambda f: "ON" if self.fault == f else "OFF"  # noqa: E731
                return f"{v:.2f},{i:.3f},{v * i:.2f},{flag('OVP')},{flag('OCP')},OFF,{mode}"
            else:
                return "ERR"
            self._check_protection()
            return None

    def _serve(self) -> None:
        master = self._master  # stop() clears the attribute from another thread
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = os.read(master, 1024)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                reply = self.handle(line.decode(errors="replace"))
                if reply is not None:
                    try:
                        os.write(master, (reply + "\n").encode())
                    except OSError:
                        return


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mp711135"
    dev = FakeMP711135(path).start()
    print(f"Fake MP711135 listening at {path} (10 ohm load). Ctrl+C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        dev.stop()
