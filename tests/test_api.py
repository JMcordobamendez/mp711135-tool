import time

import pytest
from fastapi.testclient import TestClient

from backend import config as cfg
from backend import main
from tests.fake_device import FakeMP711135


def wait_until(pred, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def port(tmp_path, monkeypatch):
    path = str(tmp_path / "mp711135")
    monkeypatch.setattr(cfg, "PORT", path)
    monkeypatch.setattr(cfg, "TIMEOUT", 0.3)
    monkeypatch.setattr(cfg, "POLL_HZ", 20)
    monkeypatch.setattr(main, "SETPOINT_SYNC_S", 0.2)
    return path


@pytest.fixture
def fake(port):
    dev = FakeMP711135(port).start()
    yield dev
    dev.stop()


@pytest.fixture
def client(fake):
    with TestClient(main.app) as c:
        yield c


def test_idn_and_state(client):
    assert "MP711135" in client.get("/idn").json()["raw"]
    s = client.get("/state").json()
    assert s["output"] is False
    assert s["voltage_limit"] == 62.0
    assert s["measurement"]["mode"] == "STANDBY"


def test_set_values_round_trip(client, fake):
    assert client.put("/voltage", json={"value": 12.5}).json() == {"voltage_setpoint": 12.5}
    assert client.put("/current", json={"value": 0.5}).json() == {"current_setpoint": 0.5}
    assert client.put("/voltage-limit", json={"value": 30}).json() == {"voltage_limit": 30.0}
    assert client.put("/current-limit", json={"value": 2}).json() == {"current_limit": 2.0}
    assert client.put("/output", json={"on": True}).json() == {"output": True}
    # 12.5 V into 10 ohm wants 1.25 A > 0.5 A limit -> constant current.
    m = client.get("/measurements").json()
    assert m["mode"] == "CC"
    assert m["current"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    "path,value", [("/voltage", 61), ("/voltage", -1), ("/current", 10.5), ("/voltage-limit", 63)]
)
def test_out_of_range_rejected(client, path, value):
    assert client.put(path, json={"value": value}).status_code == 422


def test_fault_reset(client, fake):
    client.put("/voltage", json={"value": 20})
    client.put("/voltage-limit", json={"value": 10})
    client.put("/output", json={"on": True})
    assert client.get("/measurements").json()["ovp_fault"] is True
    m = client.post("/faults/reset").json()
    assert m["ovp_fault"] is False and m["mode"] == "STANDBY"


def test_stale_reply_does_not_desync(client, fake):
    fake.inject("99.99\n")  # e.g. a reply that arrived after its query timed out
    time.sleep(0.05)
    assert client.put("/voltage", json={"value": 3.3}).json() == {"voltage_setpoint": 3.3}


def test_starts_without_device_and_recovers(port):
    with TestClient(main.app) as client:
        assert client.get("/idn").status_code == 503
        dev = FakeMP711135(port).start()
        try:
            assert wait_until(lambda: client.get("/idn").status_code == 200)
        finally:
            dev.stop()


def test_recovers_after_unplug(port, fake):
    with TestClient(main.app) as client:
        assert client.get("/idn").status_code == 200
        fake.stop()
        assert wait_until(lambda: client.get("/idn").status_code == 503)
        fake.start()
        assert wait_until(lambda: client.get("/idn").status_code == 200)


def test_websocket_streams_measurements_and_panel_changes(client, fake):
    with client.websocket_connect("/ws/measurements") as ws:
        msg = ws.receive_json()
        assert msg["device_connected"] is True
        assert "voltage" in msg and "sequence" in msg
        # Someone turns the knob on the front panel.
        with fake._lock:
            fake.voltage = 7.77
        for _ in range(60):
            msg = ws.receive_json()
            if msg.get("setpoints", {}).get("voltage_setpoint") == 7.77:
                break
        else:
            pytest.fail("front-panel change never reached the WebSocket")


def test_sequence_ramp(client, fake):
    client.put("/voltage", json={"value": 0})
    r = client.post(
        "/sequence",
        json={
            "steps": [
                {"voltage": 0, "output": True, "current": 1},
                {"voltage": 5, "ramp_s": 0.5},
                {"voltage": 2, "hold_s": 0.1},
            ],
            "repeat": 2,
        },
    )
    assert r.status_code == 200 and r.json()["running"] is True
    assert wait_until(lambda: client.get("/sequence").json()["running"] is False)
    status = client.get("/sequence").json()
    assert status["error"] is None
    volts = [float(c.split()[1]) for c in fake.commands if c.startswith("VOLTage ")]
    # A ramp is many small steps, not a jump straight to 5 V.
    assert len([v for v in volts if 0 < v < 5]) >= 4
    assert volts[-1] == 2.0
    assert fake.output is True


def test_sequence_stop(client, fake):
    client.post("/sequence", json={"steps": [{"voltage": 1, "hold_s": 30}]})
    assert client.get("/sequence").json()["running"] is True
    assert client.delete("/sequence").json()["running"] is False


def test_sequence_aborts_on_protection_trip(client, fake):
    client.put("/voltage-limit", json={"value": 5})
    r = client.post(
        "/sequence", json={"steps": [{"voltage": 0, "output": True}, {"voltage": 10, "hold_s": 5}]}
    )
    assert r.status_code == 200
    assert wait_until(lambda: client.get("/sequence").json()["running"] is False)
    assert "OVP" in client.get("/sequence").json()["error"]


def test_sequence_validation(client):
    assert client.post("/sequence", json={"steps": []}).status_code == 422
    assert client.post("/sequence", json={"steps": [{"voltage": 70}]}).status_code == 422


def test_ramp_duration_is_wall_clock(client, fake):
    # Serial round-trips must not stretch the ramp beyond ramp_s.
    t0 = time.monotonic()
    client.post("/sequence", json={"steps": [{"voltage": 10, "ramp_s": 2.0}]})
    assert wait_until(lambda: client.get("/sequence").json()["running"] is False)
    assert time.monotonic() - t0 < 2.4
