# Status at snapshot (2026-10-29)

Code last modified **2026-08-12** in the devstack VM; deployment left running
(router `grout-router` ACTIVE/UP, VM `router-vm-grout-router` ACTIVE on
192.168.254.88, `cirros-test` VM on the private net).

## Working (validated in the live deployment)

- Grout provider loaded as default `L3_ROUTER_NAT` provider via STF
- Router create/delete → Nova VM spawn/delete, mgmt network auto-created
- Router interface add/remove → port attach/detach + in-VM config (netns)
- Extra routes (add/remove) pushed to the agent (`ip route` in `grout-ns`)
- Heartbeat health monitor: UDP + HMAC, router status ACTIVE/ERROR
- Agent REST API: `/health`, `/interface`, `/route(s)`, `/floatingip`,
  `/snat`, `/portforwarding`, `/nat` (LinuxBackend: netns + nft)

## Stubbed / not wired

- **Floating IPs**: `grout.py` FLOATING_IP handlers are log-only stubs —
  `RouterVMManager.add_floating_ip/remove_floating_ip` and the agent
  endpoints exist but are never called from the registry callbacks.
- **SNAT**: manager + agent support implemented, but nothing triggers it
  (no call on gateway attach / `enable_snat` router option).
- **Port forwarding**: same — agent + manager ready, no caller.
- **`GroutBackend`** in the agent: empty; the dataplane is Linux netns/nft,
  not grout/`grcli` yet. This is the core "actually use grout" step.
- **`GroutNoLsp`**: OVN-only variant, sketch quality; current deployment is
  ML2/OVS so it cannot be exercised.
- **IPv6**: `_configure_port_on_vm` skips `:` addresses; agent and cloud-init
  are IPv4-only.

## Known rough edges

- In-memory router↔VM map + health state (single-process assumptions;
  `neutron-api` and `neutron-rpc-server` are separate processes — callbacks
  fire in whichever process handles the request, `_discover_vm` papers over
  this on the rpc-server side).
- Raw SQL `UPDATE ports SET project_id` to satisfy Nova's tenant check on
  external-port attach.
- Unauthenticated agent REST API on the mgmt network; heartbeat key is a
  shared default (`changeme`).
- `time.sleep` polling inside the plugin process (`_wait_for_vm_deleted`,
  `_wait_for_vm_active`, `_wait_for_agent`).
- Blocking `requests` calls to the agent inside API/rpc worker threads.

## Suggested next steps

1. Wire floating IP + SNAT handling into the registry callbacks (manager and
   agent sides already exist).
2. Implement `GroutBackend` on top of grout's `grcli` (interfaces, routes,
   NAT) so the dataplane is actually grout; keep `LinuxBackend` as fallback.
3. Persist the router↔VM mapping (e.g. `extra_attributes` on the router, or
   a small table) instead of process memory + Nova discovery.
4. Capture the router-VM image build (kickstart/cloud-init recipe that
   installs grout-agent + systemd unit) — currently only the baked qcow2
   (`/opt/stack/grout-router.qcow2`, Glance image `fedora-44-cloudinit`)
   exists; `code/grout-agent` + `config/grout-agent.service` are the pieces
   to reproduce it.
5. Re-evaluate `_fix_port_project_id` — a nova microversion or service-user
   path may remove the need for direct SQL.
6. Turn the PoC into a proper out-of-tree plugin package (see
   `docs/neutron-plugin-dev-guide.md` § "Creating a New Plugin") instead of
   untracked files inside the neutron tree.
