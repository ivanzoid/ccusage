"""End-to-end tests for ccusage using a fake API and tmux."""

import copy
import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _future_iso(hours: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _usage_payload(five_hour: float, seven_day: float) -> dict:
    return {
        "five_hour": {"utilization": five_hour, "resets_at": _future_iso(2)},
        "seven_day": {"utilization": seven_day, "resets_at": _future_iso(48)},
    }


def _repeat(event: dict, count: int) -> list[dict]:
    return [copy.deepcopy(event) for _ in range(count)]


class _EventScript:
    """Thread-safe event script for fake API responses."""

    def __init__(self, events: list[dict]):
        self._events = list(events)
        self._lock = threading.Lock()
        self._idx = 0
        self.requests: list[float] = []

    def next_event(self) -> dict:
        with self._lock:
            self.requests.append(time.time())
            if self._idx < len(self._events):
                event = self._events[self._idx]
                self._idx += 1
                return event
            return self._events[-1]


def _build_handler(script: _EventScript):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/api/oauth/usage":
                self.send_response(404)
                self.end_headers()
                return

            event = script.next_event()
            delay = float(event.get("delay", 0))
            if delay > 0:
                time.sleep(delay)

            status = int(event.get("status", 200))
            headers = dict(event.get("headers", {}))
            raw = event.get("raw")
            body = event.get("body", {})

            self.send_response(status)
            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"
            for k, v in headers.items():
                self.send_header(k, str(v))
            self.end_headers()

            if raw is not None:
                payload = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
                self.wfile.write(payload)
            else:
                self.wfile.write(json.dumps(body).encode("utf-8"))

        def log_message(self, *_):
            return

    return Handler


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return sock.getsockname()[1]


@unittest.skipUnless(
    shutil.which("tmux") and os.getenv("CCUSAGE_RUN_E2E") == "1",
    "Set CCUSAGE_RUN_E2E=1 and install tmux to run e2e tests.",
)
class TestCcusageE2EInTmux(unittest.TestCase):
    def setUp(self):
        self.session = f"ccusage-e2e-{os.getpid()}-{int(time.time() * 1000)}"
        self.tmux_target = f"{self.session}:0.0"
        self.tmp = tempfile.TemporaryDirectory(prefix="ccusage-e2e-")
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.creds_path = self.home / ".claude" / ".credentials.json"
        self.creds_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path = str(Path(self.tmp.name) / "state.json")
        self.server = None
        self.server_thread = None

    def tearDown(self):
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", self.session],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            self._stop_fake_api()
            self.tmp.cleanup()

    def _write_creds(self, path: Path | None = None):
        p = path or self.creds_path
        p.parent.mkdir(parents=True, exist_ok=True)
        creds = {
            "claudeAiOauth": {
                "accessToken": "e2e-token",
                "expiresAt": int((time.time() + 3600) * 1000),
            }
        }
        p.write_text(json.dumps(creds))

    def _run_tmux(self, *args: str) -> str:
        proc = subprocess.run(
            ["tmux", *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout

    def _start_fake_api(self, events: list[dict]):
        self._stop_fake_api()
        port = _free_port()
        script = _EventScript(events)
        handler = _build_handler(script)
        self.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.script = script
        self.api_base = f"http://127.0.0.1:{port}"

    def _stop_fake_api(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=2)
        self.server_thread = None

    def _start_tmux_app(
        self,
        width: int,
        height: int,
        interval: int = 1,
        api_base: str | None = None,
        credentials_path: str | None = None,
        write_credentials: bool = True,
        extra_env: dict[str, str] | None = None,
    ):
        if write_credentials:
            self._write_creds()

        env = {
            "HOME": str(self.home),
            "CCUSAGE_STATE_PATH": self.state_path,
            "CCUSAGE_API_BASE": api_base or getattr(self, "api_base", "http://127.0.0.1:9"),
            "PYTHONUNBUFFERED": "1",
        }
        if credentials_path:
            env["CCUSAGE_CREDENTIALS_PATH"] = credentials_path
        if extra_env:
            env.update(extra_env)

        env_part = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())
        cmd = f"{env_part} python3 /home/ivan/ccusage/ccusage.py --interval {interval}"
        self._run_tmux("new-session", "-d", "-s", self.session, "-x", str(width), "-y", str(height), cmd)

    def _pane_size(self) -> tuple[int, int]:
        out = self._run_tmux(
            "display-message",
            "-p",
            "-t",
            self.tmux_target,
            "#{pane_width} #{pane_height}",
        ).strip()
        w_s, h_s = out.split()
        return int(w_s), int(h_s)

    def _resize(self, width: int, height: int):
        self._run_tmux("resize-window", "-t", f"{self.session}:0", "-x", str(width), "-y", str(height))
        end = time.time() + 5
        while time.time() < end:
            if self._pane_size() == (width, height):
                return
            time.sleep(0.1)
        self.fail(f"Pane size did not reach {width}x{height}; got {self._pane_size()}")

    def _visible_lines(self) -> list[str]:
        h = int(self._run_tmux("display-message", "-p", "-t", self.tmux_target, "#{pane_height}").strip())
        captured = self._run_tmux("capture-pane", "-p", "-t", self.tmux_target, "-S", f"-{h}")
        lines = captured.splitlines()
        if len(lines) > h:
            lines = lines[-h:]
        return lines

    def _wait_for(self, predicate, timeout: float = 12.0, interval: float = 0.2):
        end = time.time() + timeout
        last = []
        while time.time() < end:
            last = self._visible_lines()
            if predicate(last):
                return last
            time.sleep(interval)
        self.fail("Condition not met in time.\n" + "\n".join(last))

    @staticmethod
    def _last_nonempty(lines: list[str]) -> str:
        for line in reversed(lines):
            if line.strip():
                return line
        return ""

    @staticmethod
    def _trailing_empty_count(lines: list[str]) -> int:
        n = 0
        for line in reversed(lines):
            if line.strip():
                break
            n += 1
        return n

    def test_interleaved_api_states_and_runtime_resize(self):
        events = [
            *_repeat({"status": 200, "body": _usage_payload(11.0, 22.0)}, 3),
            *_repeat({"status": 500, "body": {"error": "boom"}}, 3),
            *_repeat({"status": 429, "headers": {"Retry-After": "2"}, "body": {"error": {"retry_after": 2}}}, 3),
            *_repeat({"status": 200, "body": _usage_payload(73.0, 47.0)}, 3),
        ]
        self._start_fake_api(events)
        self._start_tmux_app(width=80, height=12, interval=1)

        first = self._wait_for(lambda ls: any("11%" in ln for ln in ls) and any("22%" in ln for ln in ls))
        self.assertIn("7d", self._last_nonempty(first))

        self._resize(width=57, height=5)
        err_lines = self._wait_for(lambda ls: any("API error 500" in ln for ln in ls), timeout=14.0)
        self.assertIn("7d", self._last_nonempty(err_lines))

        self._resize(width=100, height=20)
        rate_lines = self._wait_for(lambda ls: any("Rate-limited" in ln for ln in ls), timeout=14.0)
        self.assertIn("7d", self._last_nonempty(rate_lines))

        final_lines = self._wait_for(
            lambda ls: any("73%" in ln for ln in ls) and any("47%" in ln for ln in ls),
            timeout=18.0,
        )
        self.assertGreaterEqual(len(self.script.requests), 4)
        self.assertIn("7d", self._last_nonempty(final_lines))

    def test_401_then_recover(self):
        events = [
            *_repeat({"status": 401, "body": {"error": "unauthorized"}}, 2),
            *_repeat({"status": 200, "body": _usage_payload(31.0, 19.0)}, 4),
        ]
        self._start_fake_api(events)
        self._start_tmux_app(width=80, height=10, interval=1)

        self._wait_for(lambda ls: any("Unauthorized (401)" in ln for ln in ls), timeout=10.0)
        lines = self._wait_for(lambda ls: any("31%" in ln for ln in ls) and any("19%" in ln for ln in ls), timeout=12.0)
        self.assertGreaterEqual(len(self.script.requests), 3)
        self.assertIn("7d", self._last_nonempty(lines))

    def test_invalid_json_and_non_dict_then_recover(self):
        events = [
            {"status": 200, "raw": "not-json", "headers": {"Content-Type": "application/json"}},
            {"status": 200, "body": ["unexpected", "list"]},
            *_repeat({"status": 200, "body": _usage_payload(66.0, 77.0)}, 4),
        ]
        self._start_fake_api(events)
        self._start_tmux_app(width=80, height=10, interval=1)

        self._wait_for(lambda ls: any("invalid JSON" in ln for ln in ls), timeout=10.0)
        self._wait_for(lambda ls: any("unexpected response" in ln for ln in ls), timeout=10.0)
        lines = self._wait_for(lambda ls: any("66%" in ln for ln in ls) and any("77%" in ln for ln in ls), timeout=12.0)
        self.assertIn("7d", self._last_nonempty(lines))

    def test_network_error_when_server_dies(self):
        events = _repeat({"status": 200, "body": _usage_payload(25.0, 35.0)}, 20)
        self._start_fake_api(events)
        self._start_tmux_app(width=80, height=10, interval=1)

        self._wait_for(lambda ls: any("25%" in ln for ln in ls) and any("35%" in ln for ln in ls), timeout=10.0)
        self._stop_fake_api()
        err_lines = self._wait_for(lambda ls: any("Network error" in ln for ln in ls), timeout=12.0)
        self.assertIn("7d", self._last_nonempty(err_lines))

    def test_missing_credentials_file(self):
        missing = Path(self.tmp.name) / "does-not-exist" / "credentials.json"
        self._start_tmux_app(
            width=80,
            height=8,
            interval=1,
            api_base="http://127.0.0.1:9",
            credentials_path=str(missing),
            write_credentials=False,
        )

        lines = self._wait_for(lambda ls: any("credentials.json not found" in ln for ln in ls), timeout=8.0)
        self.assertTrue(any("Fetching" in ln for ln in lines))

    def test_cache_backoff_restores_then_fetches(self):
        cached = {
            "saved_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            "backoff_until": time.time() + 3.5,
            "usage": _usage_payload(10.0, 20.0),
        }
        Path(self.state_path).write_text(json.dumps(cached))

        events = _repeat({"status": 200, "body": _usage_payload(88.0, 12.0)}, 20)
        self._start_fake_api(events)
        self._start_tmux_app(width=80, height=10, interval=1)

        lines = self._wait_for(lambda ls: any("Rate-limited" in ln for ln in ls), timeout=8.0)
        self.assertTrue(any("10%" in ln for ln in lines))
        self.assertTrue(any("20%" in ln for ln in lines))

        time.sleep(1.0)
        self.assertEqual(len(self.script.requests), 0)

        final = self._wait_for(lambda ls: any("88%" in ln for ln in ls) and any("12%" in ln for ln in ls), timeout=14.0)
        self.assertGreaterEqual(len(self.script.requests), 1)
        self.assertIn("7d", self._last_nonempty(final))

    def test_resize_storm_stays_stable(self):
        events = _repeat({"status": 200, "body": _usage_payload(73.0, 47.0)}, 40)
        self._start_fake_api(events)
        self._start_tmux_app(width=80, height=12, interval=1)
        self._wait_for(lambda ls: any("73%" in ln for ln in ls) and any("47%" in ln for ln in ls), timeout=10.0)

        sizes = [(57, 5), (80, 12), (100, 20), (60, 7), (120, 10), (57, 5), (90, 15)]
        for w, h in sizes:
            self._resize(width=w, height=h)
            time.sleep(0.25)

        stable = self._wait_for(
            lambda ls: any("5h" in ln for ln in ls)
            and "7d" in self._last_nonempty(ls)
            and self._trailing_empty_count(ls) == 0,
            timeout=12.0,
        )
        self.assertIn("7d", self._last_nonempty(stable))


if __name__ == "__main__":
    unittest.main()
