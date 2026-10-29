# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import json as _json
import time

import requests
from sqlalchemy import text as sa_text
from keystoneauth1.identity import v3 as ks_v3
from keystoneauth1 import session as ks_session
from neutronclient.v2_0 import client as neutron_client
from novaclient import client as nova_client
from oslo_config import cfg
from oslo_log import log as logging

LOG = logging.getLogger(__name__)

ROUTER_VM_OPTS = [
    cfg.StrOpt('router_vm_flavor',
               default='ds1G',
               help='Nova flavor name for router VMs'),
    cfg.StrOpt('router_vm_image',
               default='fedora',
               help='Glance image name (or substring) for router VMs'),
    cfg.StrOpt('mgmt_network_name',
               default='router-mgmt-net',
               help='Management network name for router VMs'),
    cfg.StrOpt('mgmt_subnet_cidr',
               default='192.168.254.0/24',
               help='Management subnet CIDR'),
    cfg.StrOpt('mgmt_host_ip',
               default='192.168.254.1',
               help='Controller IP on the management network'),
    cfg.IntOpt('agent_port',
               default=9100,
               help='Port the grout-agent listens on in router VMs'),
    cfg.IntOpt('agent_timeout',
               default=10,
               help='HTTP request timeout for grout-agent calls'),
    cfg.StrOpt('vm_ssh_password',
               default='grout',
               help='Password for the VM user (cloud-init)'),
    cfg.BoolOpt('health_check_enabled',
                default=True,
                help='Enable periodic heartbeat-based health checking'),
    cfg.IntOpt('health_check_interval',
               default=10,
               help='Interval in seconds between heartbeat staleness checks'),
    cfg.IntOpt('health_check_max_retries',
               default=3,
               help='Consecutive stale readings before marking ERROR'),
    cfg.IntOpt('heartbeat_port',
               default=5555,
               help='UDP port the controller listens on for heartbeats'),
    cfg.StrOpt('heartbeat_key',
               default='changeme',
               help='Shared HMAC-SHA256 key for heartbeat authentication'),
    cfg.IntOpt('heartbeat_interval',
               default=5,
               help='Seconds between agent heartbeats'),
    cfg.IntOpt('heartbeat_timeout',
               default=30,
               help='Seconds without a heartbeat before considering stale'),
]

cfg.CONF.register_opts(ROUTER_VM_OPTS, 'grout')


class RouterVMManager:
    """Manages VM lifecycle for grout routers."""

    def __init__(self):
        self._nova = None
        self._neutron = None
        self._session = None
        self._mgmt_network_id = None
        self._mgmt_subnet_id = None
        self._router_vm_map = {}

    def _get_session(self):
        if self._session is None:
            auth = ks_v3.Password(
                auth_url=cfg.CONF.nova.auth_url,
                username=cfg.CONF.nova.username,
                password=cfg.CONF.nova.password,
                project_name=cfg.CONF.nova.project_name,
                user_domain_name=cfg.CONF.nova.user_domain_name,
                project_domain_name=cfg.CONF.nova.project_domain_name)
            self._session = ks_session.Session(auth=auth)
        return self._session

    def _get_nova_client(self, project_id=None):
        if project_id:
            auth = ks_v3.Password(
                auth_url=cfg.CONF.nova.auth_url,
                username=cfg.CONF.nova.username,
                password=cfg.CONF.nova.password,
                project_id=project_id,
                user_domain_name=cfg.CONF.nova.user_domain_name)
            session = ks_session.Session(auth=auth)
            return nova_client.Client('2.1', session=session)
        if self._nova is None:
            self._nova = nova_client.Client('2.1',
                                            session=self._get_session())
        return self._nova

    def _get_neutron_client(self):
        if self._neutron is None:
            self._neutron = neutron_client.Client(
                session=self._get_session())
        return self._neutron

    # ---- management network ------------------------------------------------

    def ensure_management_network(self):
        if self._mgmt_network_id:
            return self._mgmt_network_id

        nc = self._get_neutron_client()
        name = cfg.CONF.grout.mgmt_network_name

        nets = nc.list_networks(name=name)['networks']
        if nets:
            self._mgmt_network_id = nets[0]['id']
            subs = nc.list_subnets(
                network_id=self._mgmt_network_id)['subnets']
            if subs:
                self._mgmt_subnet_id = subs[0]['id']
            LOG.info('Using existing management network %s',
                     self._mgmt_network_id)
            return self._mgmt_network_id

        net = nc.create_network(
            body={'network': {'name': name,
                              'admin_state_up': True}})['network']
        self._mgmt_network_id = net['id']
        LOG.info('Created management network %s', self._mgmt_network_id)

        sub = nc.create_subnet(
            body={'subnet': {'network_id': self._mgmt_network_id,
                             'cidr': cfg.CONF.grout.mgmt_subnet_cidr,
                             'ip_version': 4,
                             'enable_dhcp': True,
                             'name': name + '-subnet'}})['subnet']
        self._mgmt_subnet_id = sub['id']
        LOG.info('Created management subnet %s', self._mgmt_subnet_id)
        return self._mgmt_network_id

    # ---- cloud-init ----------------------------------------------------------

    def _build_cloud_init(self, mgmt_ip):
        controller_ip = cfg.CONF.grout.mgmt_host_ip
        heartbeat_port = cfg.CONF.grout.heartbeat_port
        heartbeat_key = cfg.CONF.grout.heartbeat_key
        heartbeat_interval = cfg.CONF.grout.heartbeat_interval
        password = cfg.CONF.grout.vm_ssh_password

        agent_conf = _json.dumps({
            'heartbeat_target': '%s:%d' % (controller_ip, heartbeat_port),
            'heartbeat_key': heartbeat_key,
            'heartbeat_interval': heartbeat_interval,
            'mgmt_ip': mgmt_ip,
        })

        return """#cloud-config
password: %(password)s
chpasswd:
  expire: false
ssh_pwauth: true

write_files:
  - path: /etc/ssh/sshd_config.d/60-grout.conf
    content: |
      PasswordAuthentication yes

  - path: /etc/grout/agent.conf
    content: |
      %(agent_conf)s

  - path: /etc/sysctl.d/99-router.conf
    content: |
      net.ipv4.ip_forward = 1

runcmd:
  - sysctl -p /etc/sysctl.d/99-router.conf
  - systemctl restart sshd.service || systemctl restart sshd.socket
  - systemctl enable grout-agent
  - systemctl restart grout-agent
""" % {'password': password, 'agent_conf': agent_conf}

    # ---- VM lifecycle -------------------------------------------------------

    def create_router_vm(self, context, router_id, router_name=None,
                         project_id=None):
        LOG.info('Creating VM for router %s (project %s)',
                 router_id, project_id)
        mgmt_port_id = None

        try:
            nova = self._get_nova_client(project_id=project_id)
            nc = self._get_neutron_client()

            self.ensure_management_network()

            port_body = {
                'network_id': self._mgmt_network_id,
                'name': 'router-vm-mgmt-' + router_id[:8],
                'admin_state_up': True,
                'port_security_enabled': False,
                'security_groups': [],
            }
            if project_id:
                port_body['project_id'] = project_id
            mgmt_port = nc.create_port(body={'port': port_body})['port']
            mgmt_port_id = mgmt_port['id']
            mgmt_ip = mgmt_port['fixed_ips'][0]['ip_address']
            LOG.info('Created management port %s with IP %s',
                     mgmt_port_id, mgmt_ip)

            userdata = self._build_cloud_init(mgmt_ip)

            # resolve flavor
            flavor_name = cfg.CONF.grout.router_vm_flavor
            flavor_id = None
            for f in nova.flavors.list():
                if f.name == flavor_name:
                    flavor_id = f.id
                    break
            if not flavor_id:
                raise Exception('Flavor %s not found' % flavor_name)

            # resolve image
            image_hint = cfg.CONF.grout.router_vm_image
            image_id = None
            for img in nova.glance.list():
                if image_hint in img.name:
                    image_id = img.id
                    break
            if not image_id:
                raise Exception('Image matching %s not found' % image_hint)

            vm_name = 'router-vm-' + (router_name or router_id[:8])
            server = nova.servers.create(
                name=vm_name,
                image=image_id,
                flavor=flavor_id,
                nics=[{'port-id': mgmt_port_id}],
                userdata=userdata,
                config_drive=True)

            LOG.info('Created VM %s (ID: %s) for router %s',
                     vm_name, server.id, router_id)

            self._router_vm_map[router_id] = {
                'vm_id': server.id,
                'vm_name': vm_name,
                'mgmt_port_id': mgmt_port_id,
                'mgmt_ip': mgmt_ip,
                'status': 'BUILDING',
            }
            return server.id

        except Exception as e:
            LOG.error('Failed to create VM for router %s: %s', router_id, e)
            if mgmt_port_id:
                try:
                    nc.delete_port(mgmt_port_id)
                except Exception:
                    pass
            raise

    def delete_router_vm(self, context, router_id):
        LOG.info('Deleting VM for router %s', router_id)
        vm_info = self._get_vm_info(router_id)
        if not vm_info:
            self._cleanup_orphaned_ports(router_id)
            LOG.warning('No VM mapping found for router %s', router_id)
            return

        nova = self._get_nova_client()
        nc = self._get_neutron_client()
        vm_id = vm_info['vm_id']

        ports_to_delete = []
        try:
            interfaces = nova.servers.interface_list(vm_id)
            for iface in interfaces:
                ports_to_delete.append(iface.port_id)
        except Exception as e:
            LOG.warning('Could not list interfaces for VM %s: %s', vm_id, e)

        try:
            nova.servers.delete(vm_id)
            LOG.info('Deleted VM %s', vm_id)
        except Exception as e:
            LOG.error('Failed to delete VM %s: %s', vm_id, e)

        self._wait_for_vm_deleted(nova, vm_id)

        for port_id in ports_to_delete:
            try:
                nc.delete_port(port_id)
                LOG.info('Deleted port %s from router VM %s',
                         port_id, router_id)
            except Exception as e:
                LOG.debug('Port %s already deleted or not found: %s',
                          port_id, e)

        self._cleanup_orphaned_ports(router_id)
        self._router_vm_map.pop(router_id, None)

    def _wait_for_vm_deleted(self, nova, vm_id, timeout=60, interval=2):
        for _ in range(timeout // interval):
            try:
                server = nova.servers.get(vm_id)
                if server.status == 'DELETED':
                    return
            except Exception:
                return
            time.sleep(interval)

    def _cleanup_orphaned_ports(self, router_id):
        """Delete any mgmt ports left from a previous VM for this router."""
        nc = self._get_neutron_client()
        name_prefix = 'router-vm-mgmt-' + router_id[:8]
        try:
            ports = nc.list_ports(name=name_prefix)['ports']
            for port in ports:
                try:
                    nc.delete_port(port['id'])
                    LOG.info('Cleaned up orphaned mgmt port %s', port['id'])
                except Exception as e:
                    LOG.debug('Could not delete orphaned port %s: %s',
                              port['id'], e)
        except Exception as e:
            LOG.debug('Orphan port cleanup failed for router %s: %s',
                      router_id, e)

    def _wait_for_vm_active(self, vm_id, project_id=None,
                            timeout=120, interval=3):
        """Wait for a VM to reach ACTIVE state."""
        nova = self._get_nova_client(project_id=project_id)
        for _ in range(timeout // interval):
            try:
                server = nova.servers.get(vm_id)
                if server.status == 'ACTIVE':
                    LOG.info('VM %s is ACTIVE', vm_id)
                    return True
                if server.status == 'ERROR':
                    LOG.error('VM %s is in ERROR state', vm_id)
                    return False
            except Exception:
                pass
            time.sleep(interval)
        LOG.error('VM %s did not become ACTIVE within %ds', vm_id, timeout)
        return False

    def _discover_vm(self, router_id):
        """Look up VM info from Nova when in-memory cache misses."""
        try:
            nova = self._get_nova_client()
            nc = self._get_neutron_client()
            self.ensure_management_network()
            servers = nova.servers.list(
                search_opts={'all_tenants': True})
            for server in servers:
                if not server.name.startswith('router-vm-'):
                    continue
                for net_name, addrs in server.networks.items():
                    if net_name != cfg.CONF.grout.mgmt_network_name:
                        continue
                    ports = nc.list_ports(
                        device_id=server.id,
                        network_id=self._mgmt_network_id)['ports']
                    for port in ports:
                        if port.get('name', '').endswith(router_id[:8]):
                            mgmt_ip = addrs[0] if addrs else None
                            vm_info = {
                                'vm_id': server.id,
                                'vm_name': server.name,
                                'mgmt_port_id': port['id'],
                                'mgmt_ip': mgmt_ip,
                                'status': server.status,
                            }
                            self._router_vm_map[router_id] = vm_info
                            LOG.info('Discovered VM %s for router %s',
                                     server.id, router_id)
                            return vm_info
        except Exception as e:
            LOG.error('Failed to discover VM for router %s: %s',
                      router_id, e)
        return None

    def _get_vm_info(self, router_id):
        vm_info = self._router_vm_map.get(router_id)
        if vm_info:
            return vm_info
        return self._discover_vm(router_id)

    def get_router_vm(self, router_id):
        return self._get_vm_info(router_id)

    # ---- port operations ----------------------------------------------------

    def attach_port_to_vm(self, context, router_id, port_id,
                          project_id=None):
        vm_info = self._get_vm_info(router_id)
        if not vm_info:
            LOG.error('No VM found for router %s, cannot attach port',
                      router_id)
            return

        if vm_info.get('status') == 'BUILDING':
            if not self._wait_for_vm_active(vm_info['vm_id'],
                                            project_id=project_id):
                LOG.error('VM %s not active, cannot attach port',
                          vm_info['vm_id'])
                return
            vm_info['status'] = 'ACTIVE'

        nc = self._get_neutron_client()
        port = nc.show_port(port_id)['port']
        port_project = port.get('project_id') or port.get('tenant_id')

        if not port_project and project_id:
            self._fix_port_project_id(port_id, project_id)

        nc.update_port(port_id, body={'port': {
            'device_id': '',
            'device_owner': '',
            'admin_state_up': True,
            'port_security_enabled': False,
            'security_groups': [],
        }})

        nova = self._get_nova_client(project_id=project_id)
        nova.servers.interface_attach(server=vm_info['vm_id'],
                                      port_id=port_id,
                                      net_id=None, fixed_ip=None)
        LOG.info('Attached port %s to VM %s', port_id, vm_info['vm_id'])

        fixed_ips = port.get('fixed_ips', [])
        if fixed_ips:
            self._configure_port_on_vm(vm_info, port)

    def _fix_port_project_id(self, port_id, project_id):
        """Set project_id on a port via direct DB update.

        External network ports have empty project_id, but Nova requires
        port.tenant_id == instance.project_id for interface_attach.
        """
        try:
            from oslo_db.sqlalchemy import enginefacade
            engine = enginefacade.reader.get_engine()
            with engine.connect() as conn:
                conn.execute(
                    sa_text("UPDATE ports SET project_id = :pid "
                            "WHERE id = :port_id"),
                    {'pid': project_id, 'port_id': port_id})
                conn.commit()
            LOG.info('Set project_id=%s on port %s', project_id, port_id)
        except Exception as e:
            LOG.error('Failed to fix project_id on port %s: %s',
                      port_id, e)

    def _configure_port_on_vm(self, vm_info, port):
        """Configure the port's IP address on the VM via grout-agent."""
        mgmt_ip = vm_info.get('mgmt_ip')
        if not mgmt_ip:
            LOG.warning('No mgmt IP for VM %s, cannot configure port',
                        vm_info['vm_id'])
            return

        self._wait_for_agent(mgmt_ip, retries=10, interval=2)

        mac = port['mac_address']
        for fip in port.get('fixed_ips', []):
            ip = fip['ip_address']
            if ':' in ip:
                continue

            nc = self._get_neutron_client()
            subnet = nc.show_subnet(fip['subnet_id'])['subnet']
            cidr_prefix = subnet['cidr'].split('/')[1]
            gateway = subnet.get('gateway_ip')

            body = {
                'mac': mac,
                'ip_address': ip,
                'prefix_len': cidr_prefix,
            }
            if gateway:
                body['gateway'] = gateway

            try:
                self._agent_request(mgmt_ip, 'PUT', '/interface', body)
                LOG.info('Configured IP %s/%s on VM %s',
                         ip, cidr_prefix, vm_info['vm_id'])
            except Exception as e:
                LOG.error('Failed to configure IP on VM %s: %s',
                          vm_info['vm_id'], e)

    def detach_port_from_vm(self, context, router_id, port_id):
        vm_info = self._get_vm_info(router_id)
        if not vm_info:
            LOG.error('No VM found for router %s, cannot detach port',
                      router_id)
            return

        nova = self._get_nova_client()
        nova.servers.interface_detach(server=vm_info['vm_id'],
                                      port_id=port_id)
        LOG.info('Detached port %s from VM %s', port_id, vm_info['vm_id'])

    # ---- agent communication ------------------------------------------------

    def _agent_url(self, mgmt_ip, path):
        return 'http://%s:%d%s' % (mgmt_ip, cfg.CONF.grout.agent_port, path)

    def _agent_request(self, mgmt_ip, method, path, body=None):
        url = self._agent_url(mgmt_ip, path)
        timeout = cfg.CONF.grout.agent_timeout
        resp = requests.request(
            method, url, json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _wait_for_agent(self, mgmt_ip, retries=30, interval=2):
        """Wait until the grout-agent is reachable."""
        url = self._agent_url(mgmt_ip, '/health')
        timeout = cfg.CONF.grout.agent_timeout
        for attempt in range(retries):
            try:
                resp = requests.get(url, timeout=timeout)
                if resp.status_code == 200:
                    return True
            except requests.ConnectionError:
                pass
            except requests.Timeout:
                pass
            if attempt < retries - 1:
                time.sleep(interval)
        LOG.warning('grout-agent at %s not reachable after %d attempts',
                    mgmt_ip, retries)
        return False

    # ---- route management ---------------------------------------------------

    def add_route(self, context, router_id, destination, nexthop):
        vm_info = self._get_vm_info(router_id)
        if not vm_info:
            LOG.error('No VM found for router %s, cannot add route',
                      router_id)
            return
        try:
            self._agent_request(vm_info['mgmt_ip'], 'PUT', '/route',
                                {'destination': destination,
                                 'nexthop': nexthop})
            LOG.info('Added route %s via %s on router VM %s',
                     destination, nexthop, vm_info['vm_id'])
        except Exception as e:
            LOG.error('Failed to add route on router %s: %s', router_id, e)

    def delete_route(self, context, router_id, destination, nexthop):
        vm_info = self._get_vm_info(router_id)
        if not vm_info:
            LOG.error('No VM found for router %s, cannot delete route',
                      router_id)
            return
        try:
            self._agent_request(vm_info['mgmt_ip'], 'DELETE', '/route',
                                {'destination': destination,
                                 'nexthop': nexthop})
            LOG.info('Deleted route %s via %s on router VM %s',
                     destination, nexthop, vm_info['vm_id'])
        except Exception as e:
            LOG.error('Failed to delete route on router %s: %s',
                      router_id, e)

    # ---- floating IP ---------------------------------------------------------

    def add_floating_ip(self, context, router_id, floating_ip, fixed_ip):
        vm_info = self._get_vm_info(router_id)
        if not vm_info:
            LOG.error('No VM for router %s, cannot add floating IP', router_id)
            return
        try:
            self._agent_request(vm_info['mgmt_ip'], 'PUT', '/floatingip',
                                {'floating_ip': floating_ip,
                                 'fixed_ip': fixed_ip})
            LOG.info('Added floating IP %s -> %s on router %s',
                     floating_ip, fixed_ip, router_id)
        except Exception as e:
            LOG.error('Failed to add floating IP on router %s: %s',
                      router_id, e)

    def remove_floating_ip(self, context, router_id, floating_ip, fixed_ip):
        vm_info = self._get_vm_info(router_id)
        if not vm_info:
            LOG.error('No VM for router %s, cannot remove floating IP',
                      router_id)
            return
        try:
            self._agent_request(vm_info['mgmt_ip'], 'DELETE', '/floatingip',
                                {'floating_ip': floating_ip,
                                 'fixed_ip': fixed_ip})
            LOG.info('Removed floating IP %s -> %s on router %s',
                     floating_ip, fixed_ip, router_id)
        except Exception as e:
            LOG.error('Failed to remove floating IP on router %s: %s',
                      router_id, e)

    # ---- SNAT ----------------------------------------------------------------

    def enable_snat(self, context, router_id, external_ip, internal_cidr):
        vm_info = self._get_vm_info(router_id)
        if not vm_info:
            LOG.error('No VM for router %s, cannot enable SNAT', router_id)
            return
        try:
            self._agent_request(vm_info['mgmt_ip'], 'PUT', '/snat',
                                {'external_ip': external_ip,
                                 'internal_cidr': internal_cidr})
            LOG.info('Enabled SNAT %s for %s on router %s',
                     external_ip, internal_cidr, router_id)
        except Exception as e:
            LOG.error('Failed to enable SNAT on router %s: %s',
                      router_id, e)

    def disable_snat(self, context, router_id, external_ip, internal_cidr):
        vm_info = self._get_vm_info(router_id)
        if not vm_info:
            LOG.error('No VM for router %s, cannot disable SNAT', router_id)
            return
        try:
            self._agent_request(vm_info['mgmt_ip'], 'DELETE', '/snat',
                                {'external_ip': external_ip,
                                 'internal_cidr': internal_cidr})
            LOG.info('Disabled SNAT %s for %s on router %s',
                     external_ip, internal_cidr, router_id)
        except Exception as e:
            LOG.error('Failed to disable SNAT on router %s: %s',
                      router_id, e)

    # ---- port forwarding -----------------------------------------------------

    def add_port_forwarding(self, context, router_id, protocol, floating_ip,
                            external_port, internal_ip, internal_port):
        vm_info = self._get_vm_info(router_id)
        if not vm_info:
            LOG.error('No VM for router %s, cannot add port forwarding',
                      router_id)
            return
        try:
            self._agent_request(vm_info['mgmt_ip'], 'PUT', '/portforwarding',
                                {'protocol': protocol,
                                 'floating_ip': floating_ip,
                                 'external_port': external_port,
                                 'internal_ip': internal_ip,
                                 'internal_port': internal_port})
            LOG.info('Added port forwarding %s %s:%s -> %s:%s on router %s',
                     protocol, floating_ip, external_port,
                     internal_ip, internal_port, router_id)
        except Exception as e:
            LOG.error('Failed to add port forwarding on router %s: %s',
                      router_id, e)

    def remove_port_forwarding(self, context, router_id, protocol, floating_ip,
                               external_port, internal_ip, internal_port):
        vm_info = self._get_vm_info(router_id)
        if not vm_info:
            LOG.error('No VM for router %s, cannot remove port forwarding',
                      router_id)
            return
        try:
            self._agent_request(vm_info['mgmt_ip'], 'DELETE',
                                '/portforwarding',
                                {'protocol': protocol,
                                 'floating_ip': floating_ip,
                                 'external_port': external_port,
                                 'internal_ip': internal_ip,
                                 'internal_port': internal_port})
            LOG.info('Removed port forwarding %s %s:%s -> %s:%s on router %s',
                     protocol, floating_ip, external_port,
                     internal_ip, internal_port, router_id)
        except Exception as e:
            LOG.error('Failed to remove port forwarding on router %s: %s',
                      router_id, e)

    # ---- NAT listing ---------------------------------------------------------

    def list_nat(self, context, router_id):
        vm_info = self._get_vm_info(router_id)
        if not vm_info:
            LOG.error('No VM for router %s, cannot list NAT', router_id)
            return None
        try:
            resp = self._agent_request(vm_info['mgmt_ip'], 'GET', '/nat')
            return resp.get('nat')
        except Exception as e:
            LOG.error('Failed to list NAT on router %s: %s', router_id, e)
            return None
