"""scripts/network_probe.py: classification on real local sockets and the HTTP
flow against a stub UniBridge (http.server in a thread). No network access."""
from __future__ import annotations

import importlib.util
import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.schemas import ProbeBatch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "network_probe.py"


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("network_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Classification ──────────────────────────────────────────────────────────


def _loopback() -> tuple[int, str]:
    """::1 when this machine has IPv6 loopback (refuses promptly even under WSL
    mirrored networking, where a closed 127.0.0.1 port can hang instead)."""
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.bind(("::1", 0))
        s.close()
        return socket.AF_INET6, "::1"
    except OSError:
        return socket.AF_INET, "127.0.0.1"


def test_listening_port_is_open(probe):
    family, host = _loopback()
    srv = socket.socket(family, socket.SOCK_STREAM)
    srv.bind((host, 0))
    srv.listen(1)
    try:
        result, latency, error = probe.probe_tcp(host, srv.getsockname()[1], 1.0)
    finally:
        srv.close()
    assert result == "open"
    assert latency is not None and latency >= 0
    assert error == ""


def test_closed_port_is_refused(probe):
    family, host = _loopback()
    s = socket.socket(family, socket.SOCK_STREAM)
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()  # bound once, never listened: nothing there now
    result, latency, _ = probe.probe_tcp(host, port, 0.5)
    if host == "127.0.0.1":
        assert result in ("refused", "timeout")  # WSL mirrored loopback hangs
    else:
        assert result == "refused"
        assert latency is not None


def test_timeout_and_error_classification(probe, monkeypatch):
    def timeout(*_a, **_k):
        raise socket.timeout("timed out")

    monkeypatch.setattr(probe.socket, "create_connection", timeout)
    assert probe.probe_tcp("10.0.0.1", 22, 0.1) == ("timeout", None, "")

    def unreachable(*_a, **_k):
        raise OSError(113, "No route to host")

    monkeypatch.setattr(probe.socket, "create_connection", unreachable)
    assert probe.probe_tcp("10.0.0.1", 22, 0.1) == ("error", None, "No route to host")


def test_local_mismatch_matches_server_rule(probe):
    assert probe.local_mismatch("open", "timeout") == "recorded_open_but_blocked"
    assert probe.local_mismatch("blocked", "refused") == "passes_without_opening"
    assert probe.local_mismatch("blocked", "open") == "passes_without_opening"
    assert probe.local_mismatch("open", "refused") is None
    assert probe.local_mismatch("unknown", "timeout") is None


# ── HTTP flow ───────────────────────────────────────────────────────────────


class StubUniBridge:
    def __init__(self, targets, *, post_status=200, item_errors=None, mismatch=None):
        self.targets = targets
        self.post_status = post_status
        self.item_errors = item_errors or []
        self.mismatch = mismatch  # {(dst_host_id, port): mismatch}
        self.gets: list[dict] = []
        self.posts: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                url = urlparse(self.path)
                stub.gets.append({"path": url.path, "query": parse_qs(url.query),
                                  "apikey": self.headers.get("apikey")})
                if url.path != "/api/network/probe-targets":
                    return self._send(404, {"detail": "Not Found"})
                if parse_qs(url.query).get("src") == ["ghost"]:
                    return self._send(404, {"detail": "No registered host 'ghost'"})
                self._send(200, {"src_host_id": 7, "src_host_name": "batch-01",
                                 "src_ip": "10.1.0.5", "targets": stub.targets})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.posts.append({"path": self.path, "body": body,
                                   "apikey": self.headers.get("apikey")})
                if stub.post_status != 200:
                    return self._send(stub.post_status, {"detail": "boom"})
                stored = []
                for r in body["results"]:
                    stored.append({
                        "id": len(stored) + 1, "dst_host_id": r["dst_host_id"],
                        "port": r["port"], "protocol": r["protocol"], "result": r["result"],
                        # Like UniBridge: no observed_at sent means "now".
                        "observed_at": r.get("observed_at", "2026-11-06T00:00:00+00:00"),
                        "expected": "open",
                        "mismatch": (stub.mismatch or {}).get((r["dst_host_id"], r["port"])),
                    })
                self._send(200, {"stored": stored, "errors": stub.item_errors})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _target(dst_id, port, protocol="tcp", expected="open"):
    return {"dst_host_id": dst_id, "dst_host_name": f"h{dst_id}", "dst_ip": f"10.2.0.{dst_id}",
            "port": port, "protocol": protocol, "expected": expected,
            "expected_reason": "opening", "opening_id": 1}


@pytest.fixture
def run(probe, monkeypatch):
    stubs = []
    calls = []

    def fake_probe(ip, port, timeout):
        calls.append((ip, port))
        return ("timeout", None, "") if port == 9999 else ("open", 1.5, "")

    monkeypatch.setattr(probe, "probe_tcp", fake_probe)
    monkeypatch.setenv("UNIBRIDGE_API_KEY", "k-123")

    def _run(stub, *args):
        stubs.append(stub)
        out, err = io.StringIO(), io.StringIO()
        code = probe.main(["--url", stub.url, *args], out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    _run.calls = calls
    yield _run
    for s in stubs:
        s.close()


def test_reports_measurements_with_key_and_src(run):
    stub = StubUniBridge([_target(1, 5432), _target(1, 5432), _target(2, 53, "udp"),
                          _target(3, 443)])
    code, out, _ = run(stub, "--src", "batch-01")
    assert code == 0
    assert stub.gets[0]["apikey"] == "k-123"
    assert stub.gets[0]["query"]["src"] == ["batch-01"]
    assert len(stub.posts) == 1 and stub.posts[0]["path"] == "/api/network/probes"
    assert stub.posts[0]["apikey"] == "k-123"
    results = stub.posts[0]["body"]["results"]
    assert [(r["dst_host_id"], r["port"]) for r in results] == [(1, 5432), (3, 443)]  # deduped, no udp
    for r in results:
        assert r["src_host_id"] == 7 and r["protocol"] == "tcp" and r["result"] == "open"
        assert "observed_at" not in r  # UniBridge's clock decides, not the server's
    ProbeBatch.model_validate(stub.posts[0]["body"])  # what the real endpoint accepts
    assert "UDP cannot be measured" in out
    assert "h1" in out and "5432" in out


def test_batches_of_at_most_500(run):
    stub = StubUniBridge([_target(1, p) for p in range(1, 1202)])
    code, _, _ = run(stub, "--src", "batch-01", "-q")
    assert code == 0
    assert [len(p["body"]["results"]) for p in stub.posts] == [500, 500, 201]


def test_dry_run_does_not_post(run):
    stub = StubUniBridge([_target(1, 9999)])
    code, out, _ = run(stub, "--src", "batch-01", "--dry-run")
    assert code == 0
    assert stub.posts == []
    assert "recorded_open_but_blocked" in out and "dry run" in out


def test_http_failure_exits_nonzero(run):
    stub = StubUniBridge([_target(1, 22)], post_status=500)
    code, _, err = run(stub, "--src", "batch-01")
    assert code == 1
    assert "HTTP 500" in err and "boom" in err


def test_unknown_source_host(run):
    stub = StubUniBridge([])
    code, _, err = run(stub, "--src", "ghost")
    assert code == 1
    assert "No registered host" in err and "--src" in err


def test_unreachable_server_exits_nonzero(probe, monkeypatch):
    monkeypatch.setenv("UNIBRIDGE_API_KEY", "k")
    family, host = _loopback()
    s = socket.socket(family, socket.SOCK_STREAM)
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    err = io.StringIO()
    netloc = f"[{host}]" if ":" in host else host
    client = probe.Client(f"http://{netloc}:{port}", "k", timeout=1.0)
    with pytest.raises(probe.ApiError):
        client.probe_targets("x")
    assert probe.main(["--url", "", "--src", "x"], out=io.StringIO(), err=err) == 2


def test_mismatches_reported_and_fail_on_mismatch(run):
    targets = [_target(1, 22), _target(2, 9999)]
    stub = StubUniBridge(targets, mismatch={(2, 9999): "recorded_open_but_blocked"})
    code, out, _ = run(stub, "--src", "batch-01", "-q")
    assert code == 0
    assert "recorded_open_but_blocked" in out and "h2" in out
    assert "h1 " not in out  # quiet prints only the mismatches

    stub2 = StubUniBridge(targets, mismatch={(2, 9999): "recorded_open_but_blocked"})
    code, _, _ = run(stub2, "--src", "batch-01", "--fail-on-mismatch")
    assert code == 3


def test_rejected_items_exit_nonzero(run):
    stub = StubUniBridge([_target(1, 22)], item_errors=[{"index": 0, "message": "observed_at is in the future"}])
    code, _, err = run(stub, "--src", "batch-01")
    assert code == 1
    assert "h1 22/tcp" in err and "future" in err


def test_quiet_without_mismatch_prints_nothing(run):
    stub = StubUniBridge([_target(1, 22)])
    code, out, err = run(stub, "--src", "batch-01", "-q")
    assert (code, out, err) == (0, "", "")
