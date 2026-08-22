"""Server-layer regression tests. No Bluetooth hardware required."""
import json
import urllib.error
import threading
import time
import urllib.request

import pytest

from divoomcast import server


class FakePlayer:
    def __init__(self):
        self.stopped = threading.Event()

    def stop(self):
        self.stopped.set()


def _post(port, path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.status, json.loads(r.read())


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
        return r.status, json.loads(r.read())


@pytest.fixture
def running(unused_port=0):
    ctl = server.Controller("00-00-00-00-00-00")
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server._handler(ctl))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield ctl, port
    httpd.shutdown()


def test_stop_reaches_the_player_without_the_main_loop_running(running):
    """Regression: /api/stop used to be queued, but during playback the main
    thread is blocked in _play() and never drains the queue, so Stop did
    nothing until playback ended on its own."""
    ctl, port = running
    fake = FakePlayer()
    ctl.player = fake
    status, body = _post(port, "/api/stop", {})
    assert status == 202 and body["accepted"]
    # no Controller.run() loop is running here at all; it must still have fired
    assert fake.stopped.is_set(), "stop must not depend on the command queue"


def test_play_interrupts_an_in_flight_play(running):
    ctl, port = running
    fake = FakePlayer()
    ctl.player = fake
    _post(port, "/api/play", {"url": "ytsearch1:x"})
    assert fake.stopped.is_set(), "a new play must interrupt the current one"
    queued = ctl.cmds.get_nowait()
    assert queued["action"] == "play" and queued["url"] == "ytsearch1:x"


def test_play_requires_a_url(running):
    _, port = running
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(port, "/api/play", {})
    assert e.value.code == 400


def test_status_and_ping(running):
    _, port = running
    assert _get(port, "/api/ping")[1]["ok"] is True
    s = _get(port, "/api/status")[1]
    assert s["state"] == "idle" and "tx_kbps" in s


def test_cors_headers_present_for_the_extension(running):
    _, port = running
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status")
    with urllib.request.urlopen(req, timeout=3) as r:
        assert r.headers["Access-Control-Allow-Origin"] == "*"


def test_stop_is_safe_when_nothing_is_playing(running):
    _, port = running
    assert _post(port, "/api/stop", {})[0] == 202
