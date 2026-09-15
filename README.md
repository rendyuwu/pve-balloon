# pve-balloon

Lists every VM in a Proxmox VE cluster and says whether memory ballooning is actually
active on it.

The whole point is the cost: **one API call for the node list, plus one call per online
node.** A cluster with 12 nodes and 900 VMs costs 13 requests, not 900. There is no
per-VM config read in the normal path.

## Why it is that cheap

`GET /nodes/{node}/qemu` returns whatever `PVE::QemuServer::vmstatus()` produced —
`API2/Qemu.pm`'s `vmlist` pushes the hash through unfiltered — and `vmstatus()` fills in
the ballooning fields straight from the VM config, without talking to QEMU:

```perl
if ($conf->{balloon}) {
    $d->{balloon_min} = $conf->{balloon} * (1024 * 1024);
    $d->{shares} = defined($conf->{shares}) ? $conf->{shares} : $defaults->{shares};
}
```

`/cluster/resources?type=vm` is the endpoint people usually reach for, and it is the wrong
one here: it carries `maxmem` and `mem` but nothing about the balloon. It also returns LXC
containers alongside VMs, and containers have no virtio-balloon device at all.

**Caveat you should close yourself, once.** `balloon_min` and `shares` are not listed in
that endpoint's documented return schema. They are in the response because the handler does
not strip unknown keys, which is an implementation detail, not a contract. Before trusting
this tool on a cluster, run one request and look:

```bash
curl -sk -H "Authorization: PVEAPIToken=$PVE_TOKEN" \
  https://pve1.example.com:8006/api2/json/nodes/pve1/qemu \
  | jq '.data[] | select(.balloon_min) | {vmid, name, balloon_min, shares, maxmem}'
```

Empty output on a cluster you know has ballooning configured means your PVE version does not
expose those keys. Use the pmxcfs fallback at the bottom of this file instead.

## "Ballooning is active" means three different things

Pick the one you actually care about before reading the output. Proxmox enables the balloon
*device* by default, but a VM with the device is not a VM that Proxmox will ever resize —
those are separate conditions, and conflating them is the usual source of a wrong answer.

`pvestatd` only auto-balloons a VM that clears every test in
`PVE::AutoBalloon::compute_alg1`: the guest's balloon driver must be reporting, `balloon_min`
must be set, the VM must not be locked for migration, and `shares` must not be zero.

| VM config | Balloon device present | Auto-ballooned by pvestatd |
| --- | --- | --- |
| `balloon` not set | yes, this is the default | **no** — no floor, so it is skipped |
| `balloon: 0` | no, the device is omitted | no |
| `0 < balloon < memory`, `shares != 0` | yes | **yes** |
| `balloon == memory` | yes | no room to move |
| `shares: 0` | yes | no, manual `qm monitor` ballooning only |

The first row is the trap. A VM left at defaults shows a balloon device in the GUI and gets
resized by nothing.

One thing this tool cannot tell you from the cheap path: `balloon` unset and `balloon: 0`
both leave `balloon_min` absent, so they are indistinguishable in the VM list. Neither is
auto-ballooned, so the distinction only matters if you specifically want the device
inventory. Read the VM config, or use `--full`, if you need them separated.

## Setup

Python 3.9+, standard library only. No virtualenv, no dependencies.

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
python3 pve_balloon.py --selftest     # 16 cases, no cluster needed
```

`PVE_HOST` is a single node, not a cluster name. Any node will do: the `vmlist` endpoint is
declared `proxyto => 'node'`, so the node you contact forwards each query to the node that
owns the VM. That node is a single point of failure for one run and nothing more — if it is
down, point `PVE_HOST` somewhere else and the output is identical.

Port defaults to `8006`. Write your own (`pve.example.com:443`, or a full
`https://pve.example.com:443`) when a reverse proxy fronts the cluster.

## Token

Do not use `root@pam`. A read-only token is enough, because everything here is a `VM.Audit`
read:

```bash
pveum user add monitor@pve
pveum user token add monitor@pve ro --privsep 1
pveum acl modify / --tokens 'monitor@pve!ro' --role PVEAuditor
```

`--privsep 1` is the default, and it is the one that bites: a privilege-separated token has
**no** permissions until it gets its own ACL entry, and the third command is what grants it.
Skip that command and the API answers `200` with an empty list — `vmlist` filters per VM
against `VM.Audit` and says nothing about what it removed. The script prints a warning when
it sees zero VMs across online nodes for exactly this reason.

## Usage

```bash
python3 pve_balloon.py                  # every VM, every verdict
python3 pve_balloon.py --only yes       # just the ones pvestatd actually manages
python3 pve_balloon.py --json           # machine-readable, includes unreachable nodes
python3 pve_balloon.py --full           # adds the runtime guest-driver check (slow)
python3 pve_balloon.py --only yes --out ballooning.txt
```

Per-node progress (`[2/5] pve2: fetching...` and how long each node took) goes to stderr, so
`--out FILE` — or a plain `> FILE` — keeps the report clean while you still watch it work, and
the timings tell you which node is the slow one.

Verdicts:

| Verdict | Meaning |
| --- | --- |
| `yes` | floor set, `shares != 0`, room between floor and max — pvestatd manages it |
| `manual` | `shares: 0`, pinned at the floor, only a manual monitor command moves it |
| `no-range` | floor equals max memory, nothing to reclaim |
| `no-driver` | configured for ballooning but the guest driver is not reporting (`--full` only) |
| `paused` | locked for migration, so pvestatd skips it this cycle |
| `no` | no balloon floor in the config (or `balloon: 0`) |

Templates are skipped. Stopped VMs are listed, since their config is still the answer for
every verdict except `no-driver`.

Exit code is `2` if any node could not be read. Those nodes are printed under
`NOT ANSWERED for:` rather than being silently counted as having no ballooning — a node you
could not reach is a gap in the answer, not evidence of absence.

## Load, and why `--full` is opt-in

- `pveproxy` and `pvedaemon` both run `max_workers => 3`. Concurrency above three against one
  host buys no throughput and makes the web UI queue behind you. This script is sequential,
  which is already at the useful ceiling for 13 requests.
- `vmlist` is `protected => 1`, so it executes in `pvedaemon` as root rather than being served
  from a cached read. Cheap at one call per node; it would not stay cheap per VM.
- `--full` appends `full=1`, which makes the node issue a QMP `query-balloon` against every
  running VM to fill in `balloon` (bytes the guest currently holds), `ballooninfo` and
  `freemem`. That is the only way to learn whether the guest's balloon driver is actually
  loaded, and it is also where a wedged VM stalls the entire node's response. Behind a flag
  on purpose.
- Behind a reverse proxy, `--full` can trip the proxy's own read timeout (nginx defaults to 60
  seconds) and hand you a `504` that has nothing to do with Proxmox. Raise it there, or run
  `--full` one node at a time.

## Fallback: read pmxcfs directly

If the API path does not work on your version, or you want zero API load, `/etc/pve` is the
cluster filesystem: it is replicated to every node, so **one** node holds the configs of the
**whole** cluster.

```bash
ssh root@any-node 'for f in /etc/pve/nodes/*/qemu-server/*.conf; do
  printf "%s " "$f"; sed -n "/^\[/q;p" "$f" | tr "\n" " "; echo; done' | grep balloon
```

`sed -n "/^\[/q;p"` stops at the first `[snapshot]` section header. Without it you are reading
a snapshot's stale config alongside the live one.

Note that `memory` became a property string in recent PVE, where `current` is the default key
— so both `memory: 2048` and `memory: current=2048` are valid and any parser has to accept
both.

## Sources

- [qm.1 — `balloon`, `shares`, `memory`](https://pve.proxmox.com/pve-docs/qm.1.html)
- [API viewer](https://pve.proxmox.com/pve-docs/api-viewer/)
- [`PVE/AutoBalloon.pm`](https://git.proxmox.com/?p=pve-manager.git;a=blob_plain;f=PVE/AutoBalloon.pm;hb=HEAD) — the candidate filter
- [`PVE/QemuServer.pm`](https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer.pm;hb=HEAD) — `vmstatus`, and the device is added unless `balloon == 0`
- [`PVE/API2/Qemu.pm`](https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/API2/Qemu.pm;hb=HEAD) — `vmlist` returns `vmstatus` unfiltered
- [pmxcfs(8)](https://pve.proxmox.com/pve-docs/pmxcfs.8.html)
