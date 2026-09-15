#!/usr/bin/env python3
"""List every VM in a Proxmox cluster and say whether memory ballooning is active.

Cost: 1 call for the node list + 1 call per ONLINE node. No per-VM call.

`GET /nodes/{node}/qemu` returns PVE::QemuServer::vmstatus() verbatim (API2/Qemu.pm
`vmlist` pushes the hash unfiltered), and vmstatus sets `balloon_min` / `shares` from
the VM config without touching QMP:

    if ($conf->{balloon}) {
        $d->{balloon_min} = $conf->{balloon} * (1024 * 1024);
        $d->{shares} = defined($conf->{shares}) ? $conf->{shares} : $defaults->{shares};
    }

Those two keys are NOT in the endpoint's documented return schema, so treat their
presence as a fact to confirm once against your own cluster, not as a contract.
If they are missing on your version, fall back to the pmxcfs read in README-style
usage below (one ssh, zero API load):

    ssh root@<any-node> 'for f in /etc/pve/nodes/*/qemu-server/*.conf; do \
        printf "%s " "$f"; sed -n "/^\\[/q;p" "$f" | tr "\\n" " "; echo; done' \
        | grep -E 'balloon|memory'

`--full` adds `full=1`, which makes the node run a QMP `query-balloon` against every
running VM. That is the only way to learn whether the guest's balloon driver is
actually loaded, and it is also the slow path: a wedged VM stalls the whole node's
answer. Off by default.

`--ips` adds the public-IP column. vmstatus carries no address at all, so this costs
one extra call per RUNNING VM against the qemu-guest-agent
(`/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces`); a VM without the agent
answers with an error and gets a blank cell. Off by default for that reason.

Config comes from a `.env` beside this file (see .env.example): PVE_HOST
("pve1", "pve1:443", "https://pve.example.com"; port defaults to 8006),
PVE_TOKEN ("user@realm!tokenid=uuid"), optional PVE_INSECURE=1.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MIB = 1024 * 1024
ENV_FILE = Path(__file__).resolve().parent / ".env"


def log(msg: str) -> None:
    """Progress goes to stderr, so --out (or a shell `>`) still gets a clean report."""
    print(msg, file=sys.stderr, flush=True)


def load_dotenv(path: Path = ENV_FILE) -> None:
    """Read KEY=VALUE lines into os.environ. A variable already exported in the shell
    wins, so a one-off run against another cluster never needs the file edited."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        if sep:
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def base_url(host: str) -> str:
    """Accept "pve1", "pve1:443", "https://pve.example.com/", "[2001:db8::1]:443".

    Port stays whatever the operator wrote -- a cluster behind a reverse proxy answers
    on 443, not 8006 -- and only an address with no port of its own gets PVE's default.
    urlsplit does the IPv6 bracket handling so this does not have to.
    """
    parts = urllib.parse.urlsplit(host if "://" in host else f"https://{host}")
    if not parts.hostname:
        raise ValueError(f"no host in {host!r}")
    netloc = parts.netloc if parts.port else f"{parts.netloc}:8006"
    return f"{parts.scheme}://{netloc}"


def api_get(host: str, token: str, path: str, *, insecure: bool, timeout: float) -> object:
    req = urllib.request.Request(
        f"{base_url(host)}/api2/json{path}",
        headers={"Authorization": f"PVEAPIToken={token}"},
    )
    ctx = ssl._create_unverified_context() if insecure else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.load(resp)["data"]


def classify(vm: dict) -> tuple[str, str]:
    """Return (verdict, why) for one vmstatus entry.

    pvestatd only auto-balloons a VM that clears every test in
    PVE::AutoBalloon::compute_alg1: balloon driver reporting, `balloon_min` set,
    not locked for migrate, and `shares` != 0.
    """
    maxmem = vm.get("maxmem") or 0
    floor = vm.get("balloon_min")
    shares = vm.get("shares")
    runtime = vm.get("balloon")  # bytes the guest currently holds; only with full=1

    if not floor:
        # `balloon: 0` (device omitted) and an unset `balloon` (device present, no
        # floor) both land here -- vmstatus cannot tell them apart. Neither is ever
        # auto-ballooned, so the distinction only matters if you care about the
        # device itself; read the VM config or use --full to separate them.
        return "no", "balloon floor unset in config (or balloon: 0)"
    if vm.get("lock") == "migrate":
        return "paused", "locked for migrate; pvestatd skips it"
    if shares == 0:
        return "manual", f"shares=0, fixed at {floor // MIB} MiB, no auto-ballooning"
    if floor >= maxmem:
        return "no-range", f"floor {floor // MIB} MiB == max {maxmem // MIB} MiB"
    if runtime is None and "full" in vm:
        return "no-driver", "balloon device configured but guest driver not reporting"
    return "yes", f"{floor // MIB}-{maxmem // MIB} MiB, shares={shares if shares is not None else 1000}"


def public_ip(get, node: str, vmid: int) -> str:
    """First globally routable address qemu-guest-agent reports for this VM, else "".

    `is_global` is the whole filter: it already excludes loopback, link-local, RFC1918,
    CGNAT and IPv6 ULA, so there is no private-range list to keep in sync here.
    An empty cell means "no answer" (agent missing, stopped, or firewalled), NOT
    "this VM has no public address" -- the agent is the only source and it can be absent.
    """
    try:
        data = get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
    except (urllib.error.URLError, TimeoutError, OSError):
        return ""
    for iface in (data or {}).get("result", []):
        for addr in iface.get("ip-addresses", []):
            try:
                ip = ipaddress.ip_address(addr.get("ip-address", ""))
            except ValueError:
                continue
            if ip.is_global:
                return str(ip)
    return ""


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("PVE_HOST"))
    ap.add_argument("--token", default=os.environ.get("PVE_TOKEN"))
    ap.add_argument(
        "--insecure",
        action="store_true",
        default=os.environ.get("PVE_INSECURE") == "1",
        help="skip TLS verification (or PVE_INSECURE=1)",
    )
    ap.add_argument("--full", action="store_true", help="QMP query per running VM (slow)")
    ap.add_argument(
        "--ips",
        action="store_true",
        help="add the public IP column (1 guest-agent call per running VM)",
    )
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--only", default="", help="print only this verdict, e.g. --only yes")
    ap.add_argument("--out", help="write the report to this file (progress still goes to stderr)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.host or not args.token:
        ap.error(f"need --host/--token, or PVE_HOST/PVE_TOKEN in {ENV_FILE}")

    get = lambda path: api_get(  # noqa: E731
        args.host, args.token, path, insecure=args.insecure, timeout=args.timeout
    )

    # The node list is the one call with nothing to fall back on, so it reports its own
    # failure instead of ending in a traceback: a typo'd token or host lands here first.
    try:
        nodes = get("/nodes")
    except urllib.error.HTTPError as exc:
        hint = " -- check PVE_TOKEN" if exc.code == 401 else ""
        print(f"GET /nodes failed: {exc.code} {exc.reason}{hint}")
        return 1
    except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        print(f"cannot reach {base_url(args.host)}: {exc}")
        return 1

    rows, unreachable = [], []
    total, started = len(nodes), time.monotonic()
    log(f"{total} node(s)" + (" -- --full does a QMP query per running VM" if args.full else ""))
    for i, node in enumerate(sorted(nodes, key=lambda n: n["node"]), 1):
        name = node["node"]
        tag = f"[{i}/{total}] {name}"
        if node.get("status") != "online":
            log(f"{tag}: skipped, status={node.get('status')}")
            unreachable.append(f"{name} (status={node.get('status')})")
            continue
        log(f"{tag}: fetching...")
        t = time.monotonic()
        try:
            vms = get(f"/nodes/{name}/qemu" + ("?full=1" if args.full else ""))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"{tag}: FAILED after {time.monotonic() - t:.1f}s -- {exc}")
            unreachable.append(f"{name} ({exc})")
            continue
        log(f"{tag}: {len(vms)} VM(s) in {time.monotonic() - t:.1f}s")
        t = time.monotonic()
        for vm in vms:
            if vm.get("template"):
                continue
            if args.full:
                vm["full"] = True
            verdict, why = classify(vm)
            # A stopped VM has no agent to ask, so skip the call instead of paying a
            # timeout per powered-off VM.
            ip = public_ip(get, name, vm["vmid"]) if args.ips and vm.get("status") == "running" else ""
            rows.append(
                {
                    "node": name,
                    "vmid": vm["vmid"],
                    "name": vm.get("name", ""),
                    "status": vm.get("status", ""),
                    "max_mib": (vm.get("maxmem") or 0) // MIB,
                    "floor_mib": (vm.get("balloon_min") or 0) // MIB or None,
                    "shares": vm.get("shares"),
                    "ballooning": verdict,
                    "public_ip": ip,
                    "why": why,
                }
            )
        if args.ips:
            log(f"{tag}: guest-agent IPs in {time.monotonic() - t:.1f}s")

    if args.only:
        rows = [r for r in rows if r["ballooning"] == args.only]
    log(f"done in {time.monotonic() - started:.1f}s, {len(rows)} row(s)")

    out = open(args.out, "w") if args.out else sys.stdout
    try:
        if args.json:
            print(json.dumps({"vms": rows, "unreachable": unreachable}, indent=2), file=out)
        else:
            # The header says which cluster and when, because a saved report outlives the
            # shell that produced it and "which run was this?" is otherwise unanswerable.
            print(
                f"# pve-balloon  host={args.host}  {time.strftime('%Y-%m-%d %H:%M:%S%z')}  "
                f"nodes={len(nodes)}  full={'yes' if args.full else 'no'}"
                + (f"  only={args.only}" if args.only else ""),
                file=out,
            )
            head = f"{'NODE':<12} {'VMID':>6}  {'NAME':<24} {'STATUS':<8} {'BALLOON':<9} "
            if args.ips:
                head += f"{'PUBLIC IP':<15} "
            print(head + "WHY", file=out)
            for r in rows:
                ip = f"{r['public_ip'] or '-':<15} " if args.ips else ""
                print(
                    f"{r['node']:<12} {r['vmid']:>6}  {r['name'][:24]:<24} "
                    f"{r['status']:<8} {r['ballooning']:<9} {ip}{r['why']}",
                    file=out,
                )
            print(f"\n{len(rows)} VM(s) listed.", file=out)

        # A node we could not read is a gap in the answer, not an absence of ballooning.
        if unreachable:
            print("NOT ANSWERED for: " + ", ".join(unreachable), file=out)
        if not rows and nodes and not args.only:
            print(
                "0 VMs across online nodes -- if that is wrong, the API token most likely has "
                "Privilege Separation on and no VM.Audit ACL, which filters the list silently.",
                file=out,
            )
    finally:
        if out is not sys.stdout:
            out.close()
            log(f"report written to {args.out}")
    return 2 if unreachable else 0


def selftest() -> int:
    cases = [
        ({"maxmem": 4096 * MIB}, "no"),
        ({"maxmem": 4096 * MIB, "balloon_min": 2048 * MIB, "shares": 1000}, "yes"),
        ({"maxmem": 4096 * MIB, "balloon_min": 2048 * MIB, "shares": 0}, "manual"),
        ({"maxmem": 4096 * MIB, "balloon_min": 4096 * MIB, "shares": 1000}, "no-range"),
        (
            {"maxmem": 4096 * MIB, "balloon_min": 2048 * MIB, "shares": 1000, "lock": "migrate"},
            "paused",
        ),
        ({"maxmem": 4096 * MIB, "balloon_min": 2048 * MIB, "shares": 1000, "full": True}, "no-driver"),
        (
            {
                "maxmem": 4096 * MIB,
                "balloon_min": 2048 * MIB,
                "shares": 1000,
                "full": True,
                "balloon": 3000 * MIB,
            },
            "yes",
        ),
    ]
    for vm, want in cases:
        got, why = classify(vm)
        assert got == want, f"{vm} -> {got!r} ({why}), want {want!r}"

    def agent(result):
        def get(_path):
            if isinstance(result, Exception):
                raise result
            return {"result": result}

        return get

    ips = [
        ([{"ip-addresses": [{"ip-address": "203.0.113.7"}]}], ""),  # TEST-NET-3 is not global
        ([{"ip-addresses": [{"ip-address": "8.8.4.4"}]}], "8.8.4.4"),
        (
            [
                {"ip-addresses": [{"ip-address": "127.0.0.1"}, {"ip-address": "::1"}]},
                {"ip-addresses": [{"ip-address": "192.168.1.5"}, {"ip-address": "2606:4700::1"}]},
            ],
            "2606:4700::1",
        ),
        ([{"ip-addresses": [{"ip-address": "100.64.0.1"}, {"ip-address": "fe80::1"}]}], ""),
        ([{"ip-addresses": [{"ip-address": "not-an-ip"}, {"ip-address": "1.1.1.1"}]}], "1.1.1.1"),
        ([], ""),
        (urllib.error.URLError("agent not running"), ""),  # no agent -> blank, not a crash
    ]
    for result, want in ips:
        got = public_ip(agent(result), "pve1", 100)
        assert got == want, f"{result} -> {got!r}, want {want!r}"

    urls = [
        ("pve1", "https://pve1:8006"),
        ("pve1:443", "https://pve1:443"),
        ("https://pve.example.com", "https://pve.example.com:8006"),
        ("https://pve.example.com:443/", "https://pve.example.com:443"),
        ("[2001:db8::1]", "https://[2001:db8::1]:8006"),
        ("[2001:db8::1]:443", "https://[2001:db8::1]:443"),
    ]
    for host, want in urls:
        got = base_url(host)
        assert got == want, f"{host!r} -> {got!r}, want {want!r}"

    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
        fh.write(
            "# comment\n\n"
            "export PVE_HOST='pve9.example.com:443'\n"
            "PVE_TOKEN=\"me@pve!ro=deadbeef\"\n"
            "PVE_INSECURE=1\n"
            "not-a-pair\n"
        )
        env_path = Path(fh.name)
    os.environ.pop("PVE_HOST", None)
    os.environ["PVE_TOKEN"] = "already-exported"
    try:
        load_dotenv(env_path)
    finally:
        env_path.unlink()
    assert os.environ["PVE_HOST"] == "pve9.example.com:443", os.environ["PVE_HOST"]
    assert os.environ["PVE_TOKEN"] == "already-exported"  # shell wins over the file
    assert os.environ["PVE_INSECURE"] == "1"
    load_dotenv(Path("/nonexistent/.env"))  # missing file is not an error

    print(f"selftest ok ({len(cases) + len(ips) + len(urls) + 3} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
