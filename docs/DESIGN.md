# Design: Grout L3 service provider (STF PoC)

## Goal

Use the Neutron **Service Type Framework** to offer a second L3 router
implementation next to the default one: `L3_ROUTER_NAT:grout`, where each
router is backed by a dedicated Nova VM ("router VM") running a grout-style
dataplane, managed over a management network.

Routers can be selected per-flavor (STF flavors) or made the deployment-wide
default (as in the current PoC config).

## Components

```
                       Neutron server (devstack VM)
  ┌────────────────────────────────────────────────────────────┐
  │ l3 router plugin (ML2/OVS)                                 │
  │   │ registry callbacks (AFTER_CREATE / UPDATE / DELETE …)  │
  │   ▼                                                        │
  │ Grout (L3ServiceProvider)          [grout.py]              │
  │   ├─ RouterVMManager               [router_vm_manager.py]  │
  │   └─ HealthMonitor                 [health_monitor.py]     │
  └──────────────┬─────────────────────────┬───────────────────┘
                 │ Nova / Neutron APIs     │ UDP :5555 (heartbeats)
                 ▼                         │
        router VM (fedora-44-cloudinit)    │
  ┌────────────────────────────────────┐   │
  │ grout-agent (REST :9100)  ◄────────┼───┘ config via cloud-init
  │   ├─ HeartbeatSender (HMAC-SHA256) │
  │   └─ Backend (LinuxBackend)         │
  │       netns "grout-ns": ip / nft    │
  └────────────────────────────────────┘
```

### Grout provider — `code/service_providers/grout.py`

`Grout(base.L3ServiceProvider)` with `ha_support = OPTIONAL`. Subscribes to the
router/floating-ip registry events and drives the VM manager:

- **router AFTER_CREATE** → `create_router_vm()`; if the router has an external
  gateway, attach the gateway port(s) afterwards.
- **router PRECOMMIT_UPDATE** → stashes `routes_added` / `routes_removed` from
  the request body; **AFTER_UPDATE** → replays them as `add_route` /
  `delete_route` on the agent, and attaches gateway ports when
  `external_gateway_info` appears.
- **router AFTER_DELETE** → delete VM + leftover ports, clean health state.
- **ROUTER_INTERFACE AFTER_CREATE / AFTER_DELETE** → `attach_port_to_vm` /
  `detach_port_from_vm`.
- **FLOATING_IP \*** → currently log-only stubs (see STATUS.md).
- `_is_grout_provider()` decides ownership: via flavors when `flavor_id` is
  set, else via `ProviderResourceAssociation` (provider name `grout`); a
  router dict without id defaults to owned.

`GroutNoLsp(Grout)` is an OVN-flavoured variant that deletes the OVN
Logical_Switch_Port after interface creation (and recreates it before removal)
so the port exists only in Neutron/Nova, not in OVN. It relies on
`self.l3plugin._ovn_client` and is **not usable on the current ML2/OVS
deployment** — kept as a sketch for an OVN-based deployment.

### RouterVMManager — `code/service_providers/router_vm_manager.py`

Owns the router↔VM mapping (in-memory `_router_vm_map`, with a Nova
`_discover_vm()` fallback by VM/port naming convention `router-vm-<name>` /
`router-vm-mgmt-<router_id[:8]>`).

- **Management network**: `ensure_management_network()` idempotently creates
  `router-mgmt-net` (192.168.254.0/24, DHCP) — the control channel between
  the Neutron server and every router VM.
- **create_router_vm()**: create mgmt port (port security off, no SGs) →
  build cloud-init userdata (see below) → resolve flavor (`ds1G`) and image
  (`fedora-44-cloudinit`) → `nova.servers.create()` with config drive.
  On failure it cleans up the mgmt port.
- **attach_port_to_vm()**: waits for VM ACTIVE, detaches the port from the
  OVS L3 agent world (`device_id`/`device_owner` cleared, port security off),
  then `nova.servers.interface_attach()`, then programs the IP on the VM via
  the agent (`PUT /interface` with mac/ip/prefix/gateway).
  External-gateway ports have an empty `project_id`; Nova requires
  `port.tenant_id == instance.project_id`, worked around with a direct DB
  `UPDATE ports SET project_id …` (`_fix_port_project_id`).
- Route / floating IP / SNAT / port-forwarding helpers translate 1:1 into
  agent REST calls (`PUT|DELETE /route`, `/floatingip`, `/snat`,
  `/portforwarding`, `GET /nat`).

Cloud-init userdata writes `/etc/grout/agent.conf` (heartbeat target/key/
interval + mgmt IP), enables `net.ipv4.ip_forward`, sets the VM password and
enables+restarts `grout-agent`.

### HealthMonitor — `code/service_providers/health_monitor.py`

- A daemon UDP listener thread (port 5555) validates HMAC-SHA256-signed
  heartbeats from agents and records `last_heartbeat[mgmt_ip]`.
- A `neutron_worker.PeriodicWorker` (started by `Grout.__init__`, jittered
  initial delay) runs `check_health()`: lists routers owned by provider
  `grout` via `ProviderResourceAssociation`, and per router compares the
  last heartbeat against `heartbeat_timeout`. After
  `health_check_max_retries` consecutive stale readings the router status is
  set to ERROR; when heartbeats resume it is set back to ACTIVE.
- State is in-memory (`_failure_counts`, `_router_status`, `_last_heartbeat`);
  `cleanup_router()` on router delete.

### grout-agent — `code/grout-agent` (in-VM)

Single-file Python 3 REST agent (`http.server`, port 9100), installed at
`/usr/local/bin/grout-agent`, run by `grout-agent.service`.

- `HeartbeatSender`: JSON `{mgmt_ip, seq}` + HMAC-SHA256 hex digest over UDP
  to the controller every `heartbeat_interval` seconds.
- `Backend` abstraction with two implementations:
  - `LinuxBackend` (current): tenant interfaces are moved into a `grout-ns`
    network namespace (mgmt interface stays in the default namespace so the
    agent stays reachable), configured by MAC address; routes via
    `ip route` (ECMP-capable: multiple nexthops); NAT via `nft` table `ip
    grout` with `prerouting`/`postrouting` chains. Rules carry
    `grout-fip-*` / `grout-snat-*` / `grout-pf-*` comments so they can be
    listed (`GET /nat`) and deleted by comment→handle lookup.
  - `GroutBackend`: empty stub — intended to drive grout itself via `grcli`
    instead of Linux netns/nft (the actual "grout" dataplane).

## Configuration

Registered under `[grout]` in neutron.conf (see `config/neutron.conf.snippet`):

| Option | Default | Meaning |
|---|---|---|
| `router_vm_flavor` | `ds1G` | Nova flavor for router VMs |
| `router_vm_image` | `fedora` (set: `fedora-44-cloudinit`) | Glance image (substring match) |
| `mgmt_network_name` | `router-mgmt-net` | Control-plane network |
| `mgmt_subnet_cidr` | `192.168.254.0/24` | Mgmt subnet |
| `mgmt_host_ip` | `192.168.254.1` | Controller address advertised to agents |
| `agent_port` / `agent_timeout` | `9100` / `10` | Agent REST endpoint |
| `vm_ssh_password` | `grout` | cloud-injected VM password |
| `health_check_enabled` | `True` | Enable periodic checks |
| `health_check_interval` | `10` | Seconds between staleness checks |
| `health_check_max_retries` | `3` | Stale readings before ERROR |
| `heartbeat_port` / `heartbeat_key` | `5555` / `changeme` | UDP listener / HMAC key |
| `heartbeat_interval` / `heartbeat_timeout` | `5` / `30` | Agent send rate / staleness bound |

Provider registration:

```ini
[service_providers]
service_provider = L3_ROUTER_NAT:grout:neutron.services.ovn_l3.service_providers.grout.Grout:default
```

## Key data flows

**Router creation**
`POST /routers` → l3 plugin commits row → registry AFTER_CREATE →
`Grout._process_router_create` → `RouterVMManager.create_router_vm` (mgmt port,
cloud-init, Nova boot) → (if gateway set) gateway port attached + agent
`PUT /interface`.

**Interface added**
`router add subnet` → port created by plugin → ROUTER_INTERFACE AFTER_CREATE →
port detached from device ownership, `interface_attach` to router VM → agent
`PUT /interface {mac, ip, prefix, gateway}` → agent moves the NIC into
`grout-ns` and configures the address.

**Health**
agent `HeartbeatSender` → UDP :5555 → controller HMAC check →
`check_health()` periodic → router status ACTIVE/ERROR via
`l3plugin.update_router`.

## Design notes / known simplifications

- The provider code lives under `neutron/services/ovn_l3/…` for import-path
  convenience but the PoC deployment is ML2/OVS; the placement (and the
  `GroutNoLsp` variant) would need rework for a real OVN deployment.
- Router↔VM mapping and health state are in-process memory of
  `neutron-rpc-server`; `_discover_vm()` re-derives from Nova as recovery, but
  there is no DB persistence and no multi-worker coordination.
- The controller talks to agents over the tenant-shared `router-mgmt-net`;
  only heartbeats are authenticated (HMAC), the REST API on :9100 is
  unauthenticated (port security disabled on mgmt ports).
- `_fix_port_project_id` is a raw SQL workaround for Nova's tenant check on
  `interface_attach` of external-network ports.
- OpenStack clients are used from the server process (keystoneauth session
  with the `nova` section credentials), rather than a message-based agent.
