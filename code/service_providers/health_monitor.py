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

import hashlib
import hmac
import json
import socket
import threading
import time

from neutron_lib import constants as lib_const
from neutron_lib import context as n_context
from neutron_lib.plugins import constants as plugin_constants
from neutron_lib.plugins import directory
from oslo_config import cfg
from oslo_log import log as logging

from neutron.objects import servicetype as st_obj

LOG = logging.getLogger(__name__)


class HealthMonitor:
    """UDP heartbeat health monitor for grout router VMs.

    Listens for UDP heartbeats from grout-agent instances running in
    router VMs. Each heartbeat carries the VM's management IP and an
    HMAC-SHA256 signature. A periodic check marks routers ERROR when
    heartbeats stop arriving, and ACTIVE when they resume.
    """

    def __init__(self, vm_manager):
        self._vm_manager = vm_manager
        self._failure_counts = {}
        self._router_status = {}
        self._last_heartbeat = {}
        self._listener_started = False
        self._lock = threading.Lock()

    @property
    def _l3plugin(self):
        return directory.get_plugin(plugin_constants.L3)

    def _start_listener(self):
        if self._listener_started:
            return
        self._listener_started = True
        port = cfg.CONF.grout.heartbeat_port
        t = threading.Thread(target=self._listen, args=(port,), daemon=True)
        t.start()
        LOG.info('Heartbeat UDP listener started on port %d', port)

    def _listen(self, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', port))
        key = cfg.CONF.grout.heartbeat_key.encode()
        while True:
            try:
                data, addr = sock.recvfrom(4096)
                if len(data) < 64:
                    continue
                payload = data[:-64]
                received_mac = data[-64:]
                expected_mac = hmac.new(
                    key, payload, hashlib.sha256).hexdigest().encode()
                if not hmac.compare_digest(received_mac, expected_mac):
                    LOG.warning('Invalid heartbeat HMAC from %s', addr[0])
                    continue
                msg = json.loads(payload)
                mgmt_ip = msg.get('mgmt_ip')
                if mgmt_ip:
                    with self._lock:
                        self._last_heartbeat[mgmt_ip] = time.time()
            except Exception as e:
                LOG.warning('Heartbeat receive error: %s', e)

    def check_health(self):
        if not cfg.CONF.grout.health_check_enabled:
            return

        self._start_listener()

        context = n_context.get_admin_context()
        router_ids = self._get_grout_router_ids(context)
        if not router_ids:
            return

        now = time.time()
        timeout = cfg.CONF.grout.heartbeat_timeout

        for router_id in router_ids:
            try:
                self._check_one_router(context, router_id, now, timeout)
            except Exception:
                LOG.exception('Error checking router %s', router_id)

    def _get_grout_router_ids(self, context):
        assocs = st_obj.ProviderResourceAssociation.get_objects(
            context, provider_name='grout')
        return [a.resource_id for a in assocs]

    def _check_one_router(self, context, router_id, now, timeout):
        vm_info = self._vm_manager.get_router_vm(router_id)
        if not vm_info:
            return

        if vm_info.get('status') == 'BUILDING':
            return

        mgmt_ip = vm_info.get('mgmt_ip')
        if not mgmt_ip:
            return

        with self._lock:
            last = self._last_heartbeat.get(mgmt_ip)

        if last is not None and (now - last) <= timeout:
            self._on_success(context, router_id)
        elif last is None:
            pass
        else:
            self._on_failure(context, router_id, mgmt_ip, now - last)

    def _on_success(self, context, router_id):
        old_count = self._failure_counts.get(router_id, 0)
        self._failure_counts[router_id] = 0

        if self._router_status.get(router_id) == 'ERROR':
            LOG.info('Router %s recovered, setting status ACTIVE', router_id)
            self._update_router_status(
                context, router_id, lib_const.ROUTER_STATUS_ACTIVE)
            self._router_status[router_id] = 'ACTIVE'
        elif old_count > 0:
            LOG.info('Router %s heartbeat resumed after %d failure(s)',
                     router_id, old_count)

    def _on_failure(self, context, router_id, mgmt_ip, age):
        count = self._failure_counts.get(router_id, 0) + 1
        self._failure_counts[router_id] = count
        max_retries = cfg.CONF.grout.health_check_max_retries

        LOG.warning('Router %s heartbeat stale for peer %s '
                    '(%.0fs ago, %d/%d)',
                    router_id, mgmt_ip, age, count, max_retries)

        if (count >= max_retries and
                self._router_status.get(router_id) != 'ERROR'):
            LOG.error('Router %s marked ERROR after %d consecutive '
                      'heartbeat failures', router_id, count)
            self._update_router_status(
                context, router_id, lib_const.ROUTER_STATUS_ERROR)
            self._router_status[router_id] = 'ERROR'

    def _update_router_status(self, context, router_id, status):
        try:
            self._l3plugin.update_router(
                context, router_id,
                {'router': {'status': status}})
            LOG.info('Updated router %s status to %s', router_id, status)
        except Exception:
            LOG.exception('Failed to update status for router %s', router_id)

    def cleanup_router(self, router_id):
        self._failure_counts.pop(router_id, None)
        self._router_status.pop(router_id, None)
