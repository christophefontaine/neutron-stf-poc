# neutron-stf-poc

Proof of concept: Neutron routers implemented as **grout router VMs**, plugged
into Neutron through the **Service Type Framework** (STF) as an L3 service
provider.

Instead of the reference implementation (OVN distributed router or the OVS/L3
agent), each Neutron router gets a dedicated Nova VM running
[grout](https://github.com/DPDK/grout)-style dataplane logic. The Neutron server
spawns the VM, attaches the router's ports to it, and programs it (interfaces,
routes, NAT) through a small REST agent running inside the VM.

## Layout

```
neutron-stf-poc/
├── code/
│   ├── service_providers/        # Neutron-side code (lives untracked in
│   │   ├── grout.py              #   /opt/stack/neutron/neutron/services/
│   │   │                         #   ovn_l3/service_providers/ in the VM)
│   │   ├── router_vm_manager.py  #   Nova/Neutron VM lifecycle + agent client
│   │   ├── health_monitor.py     #   UDP heartbeat health monitor
│   │   └── grout.py.backup       #   older snapshot (2026-08-06)
│   └── grout-agent               # in-VM REST agent (was /usr/local/bin/
│                                 #   grout-agent inside the router VM image)
├── config/
│   ├── local.conf                # devstack local.conf (VM: /opt/stack/devstack)
│   ├── neutron.conf.snippet      # service_provider + [grout] sections
│   └── grout-agent.service       # systemd unit baked into the router VM image
└── docs/
    ├── DESIGN.md                 # architecture and data flows
    ├── STATUS.md                 # what works, what's stubbed, next steps
    └── neutron-plugin-dev-guide.md  # environment notes written during the PoC
```

## Environment

The PoC runs in the Parallels VM `devstack` (Fedora ARM64):

- `ssh devstack` → parallels@192.168.40.11; stack user: `sudo su - stack`
- Credentials: `source /opt/stack/devstack/openrc admin admin` (password `secret`)
- Devstack 2026.2, ML2/OVS, VXLAN tenant networks, systemd units
- Live at time of capture: router `grout-router` (ACTIVE/UP) backed by VM
  `router-vm-grout-router` on `router-mgmt-net` (192.168.254.0/24)
- Router VM image: `fedora-44-cloudinit` (grout-agent + systemd unit baked in;
  `/opt/stack/grout-router.qcow2` is the source qcow2)

## Quick restart of a dev cycle

```bash
ssh devstack
sudo su - stack
# edit code in /opt/stack/neutron/neutron/services/ovn_l3/service_providers/
sudo systemctl restart devstack@neutron-api devstack@neutron-rpc-server
journalctl -u devstack@neutron-api -f
```

This folder is a snapshot for design work; the VM remains the live environment.
