#!/usr/bin/env python3
"""Test, from this server, the firewall openings UniBridge has on record for it.

Asks UniBridge which ports this host should be able to reach (every recorded
opening whose source covers it, closed ones included), tries a TCP connection
to each one, and reports the results back. UniBridge compares each result with
the records and flags where they disagree (an opening recorded as open that
times out, or a path with no opening that gets through).

Python 3.6+ standard library only, so it runs as-is on air-gapped servers.

Results:
  open     connected
  refused  reached the host but nothing listens there (the firewall let it through)
  timeout  no answer — what a firewall that drops the packets looks like
  error    anything else (DNS failure, no route to host, ...)

UDP targets are skipped: an unanswered UDP packet means nothing.

Usage:
  export UNIBRIDGE_URL=https://10.0.0.5:8443
  export UNIBRIDGE_API_KEY=...        # key granted network-api and network-write-api
  python3 network_probe.py                  # measure and report
  python3 network_probe.py --dry-run        # measure and print only
  python3 network_probe.py --src batch-01 --cacert /etc/pki/unibridge-ca.pem

  --src defaults to this host's primary IP (falling back to its hostname); it
  must match a host registered in the network map, by IP or name.

Cron (quiet: prints only errors and mismatches, so cron mails only those):
  15 * * * * UNIBRIDGE_URL=https://10.0.0.5:8443 UNIBRIDGE_API_KEY=... \\
      /usr/bin/python3 /opt/unibridge/network_probe.py -q

Exit status: 0 done (mismatches are reported, not failures), 1 a request to
UniBridge failed or it rejected results, 2 bad arguments, 3 mismatches found
with --fail-on-mismatch.
"""
import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_MISMATCH = 3

BATCH_SIZE = 500  # POST /network/probes accepts at most 500 results at a time
HTTP_TIMEOUT = 30.0
PASSED_RESULTS = ("open", "refused")  # the packet got through the firewall

TARGETS_PATH = "/api/network/probe-targets"
PROBES_PATH = "/api/network/probes"


class ApiError(Exception):
    def __init__(self, message, status=None):
        Exception.__init__(self, message)
        self.status = status


# ── Measuring ───────────────────────────────────────────────────────────────


def primary_ip():
    """The address this host uses for outbound traffic, or None.

    Connecting a UDP socket sends nothing; it only makes the kernel pick the
    route and therefore the source address.
    """
    for family, probe_addr in ((socket.AF_INET, ("10.255.255.255", 1)),
                               (socket.AF_INET6, ("fd00::1", 1))):
        sock = None
        try:
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.connect(probe_addr)
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127.") and ip not in ("::1", "0.0.0.0", "::"):
                return ip
        except OSError:
            pass
        finally:
            if sock is not None:
                sock.close()
    return None


def default_src():
    return primary_ip() or socket.gethostname()


def probe_tcp(ip, port, timeout):
    """Try one TCP connection; return (result, latency_ms, error)."""
    start = time.monotonic()
    try:
        conn = socket.create_connection((ip, port), timeout=timeout)
    except ConnectionRefusedError:
        return "refused", _elapsed_ms(start), ""
    except (socket.timeout, TimeoutError):
        return "timeout", None, ""
    except OSError as exc:
        return "error", None, _describe(exc)
    latency = _elapsed_ms(start)
    try:
        conn.close()
    except OSError:
        pass
    return "open", latency, ""


def _elapsed_ms(start):
    return round((time.monotonic() - start) * 1000.0, 2)


def _describe(exc):
    text = exc.strerror or str(exc) or type(exc).__name__
    return text[:255]


def local_mismatch(expected, result):
    """The server's rule, for --dry-run where nothing is reported."""
    if expected == "open" and result == "timeout":
        return "recorded_open_but_blocked"
    if expected == "blocked" and result in PASSED_RESULTS:
        return "passes_without_opening"
    return None


def unique_targets(targets):
    seen = set()
    out = []
    for target in targets:
        key = (target.get("dst_host_id"), target.get("port"), target.get("protocol"))
        if key in seen:
            continue
        seen.add(key)
        out.append(target)
    return out


def measure(targets, timeout, concurrency):
    """Probe every TCP target; return (measurements, skipped udp targets).

    A measurement is the target dict plus result, latency_ms, error and
    observed_at (UTC ISO-8601, taken when the attempt started).
    """
    tcp = [t for t in targets if t.get("protocol", "tcp") == "tcp"]
    skipped = [t for t in targets if t.get("protocol", "tcp") != "tcp"]

    def run(target):
        observed = utc_now_iso()
        result, latency, error = probe_tcp(target["dst_ip"], int(target["port"]), timeout)
        row = dict(target)
        row.update(result=result, latency_ms=latency, error=error, observed_at=observed)
        return row

    if not tcp:
        return [], skipped
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(tcp)))) as pool:
        measurements = list(pool.map(run, tcp))
    return measurements, skipped


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Talking to UniBridge ────────────────────────────────────────────────────


def ssl_context(cacert=None, insecure=False):
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=cacert)


class Client(object):
    def __init__(self, base_url, api_key, context=None, timeout=HTTP_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.context = context
        self.timeout = timeout

    def request(self, method, path, query=None, body=None):
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"apikey": self.api_key, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        kwargs = {"timeout": self.timeout}
        if url.startswith("https:") and self.context is not None:
            kwargs["context"] = self.context
        try:
            with urllib.request.urlopen(req, **kwargs) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise ApiError("{} {} -> HTTP {}: {}".format(
                method, path, exc.code, _error_detail(exc)), status=exc.code)
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ApiError("{} {} failed: {}".format(method, url, reason))
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            raise ApiError("{} {}: response is not JSON".format(method, path))

    def probe_targets(self, src):
        return self.request("GET", TARGETS_PATH, query={"src": src})

    def report(self, results):
        return self.request("POST", PROBES_PATH, body={"results": results})


def _error_detail(exc):
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - best effort for a message
        return exc.reason
    try:
        detail = json.loads(raw).get("detail")
    except (ValueError, AttributeError):
        detail = None
    if detail is None:
        detail = raw.strip()[:300] or exc.reason
    elif not isinstance(detail, str):
        detail = json.dumps(detail)[:300]
    return detail


def report_payload(src_host_id, measurement):
    item = {
        "src_host_id": src_host_id,
        "dst_host_id": measurement["dst_host_id"],
        "port": int(measurement["port"]),
        "protocol": measurement.get("protocol", "tcp"),
        "result": measurement["result"],
        # No observed_at: results go out seconds after they were taken, and
        # UniBridge's clock decides. A server clock running fast would otherwise
        # have every result refused as "in the future", one running slow have
        # them lose to older reports.
    }
    if measurement.get("latency_ms") is not None:
        item["latency_ms"] = measurement["latency_ms"]
    if measurement.get("error"):
        item["error"] = measurement["error"]
    return item


def send_results(client, src_host_id, measurements, batch_size=BATCH_SIZE):
    """POST in batches; return (stored rows, [(measurement, message)] rejected)."""
    stored = []
    rejected = []
    for start in range(0, len(measurements), batch_size):
        chunk = measurements[start:start + batch_size]
        resp = client.report([report_payload(src_host_id, m) for m in chunk]) or {}
        stored.extend(resp.get("stored") or [])
        for err in resp.get("errors") or []:
            index = err.get("index")
            item = chunk[index] if isinstance(index, int) and 0 <= index < len(chunk) else {}
            rejected.append((item, err.get("message", "")))
    return stored, rejected


# ── Output ──────────────────────────────────────────────────────────────────


def merge_server_view(measurements, stored):
    """Take expected/mismatch from what the server stored for each path."""
    by_key = {}
    for row in stored:
        by_key[(row.get("dst_host_id"), row.get("port"), row.get("protocol"))] = row
    for m in measurements:
        row = by_key.get((m.get("dst_host_id"), m.get("port"), m.get("protocol", "tcp")))
        if row is not None:
            m["expected"] = row.get("expected", m.get("expected"))
            m["mismatch"] = row.get("mismatch")
            if row.get("observed_at") and row.get("result") != m["result"]:
                m["note"] = "newer result on record: {}".format(row.get("result"))
        else:
            m["mismatch"] = local_mismatch(m.get("expected"), m["result"])
    return measurements


def format_table(rows):
    headers = ("DESTINATION", "IP", "PORT", "PROTO", "RESULT", "MS", "EXPECTED", "MISMATCH")
    lines = []
    for m in rows:
        latency = m.get("latency_ms")
        mismatch = m.get("mismatch") or ""
        if m.get("error"):
            mismatch = (mismatch + " " if mismatch else "") + "({})".format(m["error"])
        if m.get("note"):
            mismatch = (mismatch + " " if mismatch else "") + "[{}]".format(m["note"])
        lines.append((
            str(m.get("dst_host_name", m.get("dst_host_id", ""))),
            str(m.get("dst_ip", "")),
            str(m.get("port", "")),
            str(m.get("protocol", "tcp")),
            m.get("result", ""),
            "" if latency is None else "{:.1f}".format(latency),
            str(m.get("expected", "")),
            mismatch,
        ))
    widths = [len(h) for h in headers]
    for line in lines:
        widths = [max(w, len(cell)) for w, cell in zip(widths, line)]
    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    for line in lines:
        out.append("  ".join(c.ljust(w) for c, w in zip(line, widths)).rstrip())
    return "\n".join(out)


def _sort_key(m):
    return (str(m.get("dst_host_name", "")).lower(), int(m.get("port", 0)))


# ── Main ────────────────────────────────────────────────────────────────────


def build_parser():
    parser = argparse.ArgumentParser(
        description="Test this server's recorded firewall openings and report to UniBridge.",
        epilog="Environment: UNIBRIDGE_URL, UNIBRIDGE_API_KEY.",
    )
    parser.add_argument("--url", default=os.environ.get("UNIBRIDGE_URL"),
                        help="UniBridge base URL, e.g. https://10.0.0.5:8443 (env UNIBRIDGE_URL)")
    parser.add_argument("--src", default=None,
                        help="this host's name or IP as registered (default: primary IP, else hostname)")
    parser.add_argument("--timeout", type=float, default=3.0,
                        help="seconds to wait for each connection (default 3)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="connections tried at once (default 8)")
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument("--cacert", help="CA bundle that signed UniBridge's certificate")
    tls.add_argument("--insecure", action="store_true", help="do not verify UniBridge's certificate")
    parser.add_argument("--dry-run", action="store_true", help="measure and print, do not report")
    parser.add_argument("--fail-on-mismatch", action="store_true",
                        help="exit 3 when a result contradicts the records")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="print only errors and mismatches (for cron)")
    return parser


def main(argv=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    api_key = os.environ.get("UNIBRIDGE_API_KEY", "")
    if not args.url:
        err.write("UNIBRIDGE_URL (or --url) is required\n")
        return EXIT_USAGE
    if not api_key:
        err.write("UNIBRIDGE_API_KEY is required\n")
        return EXIT_USAGE
    if args.timeout <= 0 or args.concurrency < 1:
        err.write("--timeout must be > 0 and --concurrency >= 1\n")
        return EXIT_USAGE

    def say(text):
        if not args.quiet:
            out.write(text + "\n")

    try:
        context = ssl_context(args.cacert, args.insecure)
    except (OSError, ssl.SSLError) as exc:
        err.write("cannot load --cacert: {}\n".format(exc))
        return EXIT_USAGE
    client = Client(args.url, api_key, context)
    src = args.src or default_src()

    try:
        plan = client.probe_targets(src) or {}
    except ApiError as exc:
        err.write("{}\n".format(exc))
        if exc.status == 404:
            err.write("register this host in the network map, or pass --src with its name or IP\n")
        return EXIT_FAILURE

    src_host_id = plan.get("src_host_id")
    targets = unique_targets(plan.get("targets") or [])
    say("source: {} ({}), {} target(s)".format(
        plan.get("src_host_name", src), plan.get("src_ip", ""), len(targets)))

    measurements, skipped = measure(targets, args.timeout, args.concurrency)
    for t in skipped:
        say("skipped {} {}/{}: UDP cannot be measured reliably".format(
            t.get("dst_host_name", ""), t.get("port"), t.get("protocol")))
    if not measurements:
        say("nothing to test")
        return EXIT_OK

    status = EXIT_OK
    if args.dry_run:
        merge_server_view(measurements, [])
    else:
        try:
            stored, rejected = send_results(client, src_host_id, measurements)
        except ApiError as exc:
            err.write("{}\n".format(exc))
            return EXIT_FAILURE
        merge_server_view(measurements, stored)
        for item, message in rejected:
            err.write("rejected {} {}/{}: {}\n".format(
                item.get("dst_host_name", "?"), item.get("port", "?"),
                item.get("protocol", "?"), message))
        if rejected:
            status = EXIT_FAILURE

    measurements.sort(key=_sort_key)
    mismatches = [m for m in measurements if m.get("mismatch")]
    say(format_table(measurements))
    if mismatches:
        if args.quiet:
            out.write("mismatches from {}:\n".format(plan.get("src_host_name", src)))
            out.write(format_table(mismatches) + "\n")
        else:
            out.write("{} mismatch(es) with the records\n".format(len(mismatches)))
    counts = {}
    for m in measurements:
        counts[m["result"]] = counts.get(m["result"], 0) + 1
    say("{}: {}".format(
        "measured (dry run, not reported)" if args.dry_run else "reported",
        ", ".join("{} {}".format(n, r) for r, n in sorted(counts.items()))))
    if status == EXIT_OK and mismatches and args.fail_on_mismatch:
        return EXIT_MISMATCH
    return status


if __name__ == "__main__":
    sys.exit(main())
