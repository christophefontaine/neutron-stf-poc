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


import secrets

from neutron_lib.api import validators
from neutron_lib.callbacks import events
from neutron_lib.callbacks import priority_group
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib import constants as const
from neutron_lib.db import api as db_api
from neutron_lib.exceptions import l3 as l3_exc
from neutron_lib.plugins import constants as plugin_constants
from neutron_lib.plugins import directory
from oslo_config import cfg
from oslo_log import log as logging

from neutron.db import l3_attrs_db
from neutron.db import servicetype_db as st_db
from neutron.objects import router
from neutron.services.l3_router.service_providers import base
from neutron.services.ovn_l3.service_providers import health_monitor
from neutron.services.ovn_l3.service_providers import router_vm_manager
from neutron import worker as neutron_worker


LOG = logging.getLogger(__name__)


@registry.has_registry_receivers
class Grout(base.L3ServiceProvider):
    ha_support = base.OPTIONAL

    def __init__(self, l3_plugin):
        super().__init__(l3_plugin)
        self._grout_provider = __name__ + "." + self.__class__.__name__
        self.vm_manager = router_vm_manager.RouterVMManager()
        self._pending_route_changes = {}
        self.health_mon = health_monitor.HealthMonitor(self.vm_manager)
        self._start_health_monitor()
        LOG.info('Grout __init__ with VM manager and health monitor')

    def _start_health_monitor(self):
        if not cfg.CONF.grout.health_check_enabled:
            LOG.info('Grout health monitor disabled by config')
            return
        interval = cfg.CONF.grout.health_check_interval
        initial_delay = secrets.SystemRandom().randint(0, interval)
        worker = neutron_worker.PeriodicWorker(
            self.health_mon.check_health, interval, initial_delay)
        self.l3plugin.add_worker(worker)
        LOG.info('Grout health monitor started (interval=%ds)', interval)

    @property
    def _flavor_plugin(self):
        try:
            return self._flavor_plugin_ref
        except AttributeError:
            self._flavor_plugin_ref = directory.get_plugin(
                plugin_constants.FLAVORS)
            return self._flavor_plugin_ref

    def _is_grout_provider(self, context, router):
        flavor_id = router.get('flavor_id')
        if flavor_id and flavor_id is not const.ATTR_NOT_SPECIFIED:
            flavor = self._flavor_plugin.get_flavor(context, flavor_id)
            provider = self._flavor_plugin.get_flavor_next_provider(
                context, flavor['id'])[0]
            return str(provider['driver']) == self._grout_provider

        router_id = router.get('id')
        if not router_id:
            return True

        stm = st_db.ServiceTypeManager.get_instance()
        provider_name = stm.get_provider_names_by_resource_ids(
            context, [router_id]).get(router_id)
        return provider_name == 'grout'

    @registry.receives(resources.ROUTER_CONTROLLER,
                       [events.PRECOMMIT_ADD_ASSOCIATION])
    def _process_router_add_association(self, resource, event, trigger,
                                        payload=None):
        router = payload.states[0]
        context = payload.context
        if not self._is_grout_provider(context, router):
            return
        LOG.debug('Got request to associate user defined flavor to router %s',
                  router)

    def _is_ha(self, router):
        ha = router.get('ha')
        if not validators.is_attr_set(ha):
            ha = cfg.CONF.l3_ha
        return ha

    @registry.receives(resources.ROUTER, [events.PRECOMMIT_CREATE],
                       priority_group.PRIORITY_ROUTER_EXTENDED_ATTRIBUTE)
    def _process_precommit_router_create(self, resource, event, trigger,
                                         payload):
        router = payload.latest_state
        context = payload.context
        if not self._is_grout_provider(context, router):
            return
        router_db = payload.metadata['router_db']
        is_ha = self._is_ha(router)
        router['ha'] = is_ha
        l3_attrs_db.ExtraAttributesMixin.set_extra_attr_value(router_db,
                                                              'ha', is_ha)

    def _get_router_project_id(self, router):
        return router.get('project_id') or router.get('tenant_id')

    def _attach_gateway_ports(self, context, router_id, project_id):
        """Find and attach gateway port(s) to the router VM."""
        nc = self.vm_manager._get_neutron_client()
        ports = nc.list_ports(
            device_id=router_id,
            device_owner='network:router_gateway')['ports']
        for port in ports:
            LOG.info('Attaching gateway port %s to router VM %s',
                     port['id'], router_id)
            try:
                self.vm_manager.attach_port_to_vm(
                    context, router_id, port['id'],
                    project_id=project_id)
            except Exception as e:
                LOG.error('Failed to attach gateway port %s: %s',
                          port['id'], e)

    @registry.receives(resources.ROUTER, [events.AFTER_CREATE])
    def _process_router_create(self, resource, event, trigger, payload=None):
        router = payload.states[0]
        context = payload.context
        if not self._is_grout_provider(context, router):
            return
        project_id = self._get_router_project_id(router)
        LOG.info('Creating VM for grout router %s', router['id'])
        try:
            vm_id = self.vm_manager.create_router_vm(
                context, router['id'], router.get('name'),
                project_id=project_id)
            LOG.info('Successfully created VM %s for router %s',
                     vm_id, router['id'])
        except Exception as e:
            LOG.error('Failed to create VM for router %s: %s',
                      router['id'], e)
            return

        if router.get('external_gateway_info'):
            self._attach_gateway_ports(context, router['id'], project_id)

    @registry.receives(resources.ROUTER, [events.PRECOMMIT_UPDATE])
    def _process_precommit_router_update(self, resource, event, trigger,
                                         payload=None):
        context = payload.context
        router_id = payload.resource_id
        if not self._is_grout_provider(context,
                                       payload.states[0] if payload.states
                                       else {'id': router_id}):
            return
        request_body = payload.request_body or {}
        routes_added = request_body.get('routes_added', [])
        routes_removed = request_body.get('routes_removed', [])
        if routes_added or routes_removed:
            self._pending_route_changes[router_id] = {
                'added': routes_added,
                'removed': routes_removed,
            }

    @registry.receives(resources.ROUTER, [events.AFTER_UPDATE])
    def _process_router_update(self, resource, event, trigger, payload=None):
        original = payload.states[0]
        context = payload.context
        if not self._is_grout_provider(context, original):
            return
        router_id = payload.resource_id
        updated = payload.states[1] if len(payload.states) > 1 else {}

        old_gw = original.get('external_gateway_info')
        new_gw = updated.get('external_gateway_info')
        project_id = self._get_router_project_id(
            updated if updated else original)

        if not old_gw and new_gw:
            LOG.info('Gateway added to router %s, attaching port', router_id)
            self._attach_gateway_ports(context, router_id, project_id)
        elif old_gw and not new_gw:
            LOG.info('Gateway removed from router %s', router_id)

        changes = self._pending_route_changes.pop(router_id, None)
        if not changes:
            return

        for route in changes['added']:
            LOG.info('Adding route %s via %s on router %s',
                     route['destination'], route['nexthop'], router_id)
            self.vm_manager.add_route(
                context, router_id,
                route['destination'], route['nexthop'])

        for route in changes['removed']:
            LOG.info('Removing route %s via %s from router %s',
                     route['destination'], route['nexthop'], router_id)
            self.vm_manager.delete_route(
                context, router_id,
                route['destination'], route['nexthop'])

    @registry.receives(resources.ROUTER, [events.AFTER_DELETE])
    def _process_router_delete(self, resource, event, trigger, payload=None):
        router_id = payload.resource_id
        LOG.info('Deleting VM for grout router %s', router_id)
        try:
            self.vm_manager.delete_router_vm(payload.context, router_id)
            LOG.info('Successfully deleted VM for router %s', router_id)
        except Exception as e:
            LOG.error('Failed to delete VM for router %s: %s', router_id, e)
        self.health_mon.cleanup_router(router_id)

    @registry.receives(resources.ROUTER_INTERFACE, [events.AFTER_CREATE])
    def _process_add_router_interface(self, resource, event, trigger, payload):
        router = payload.states[0]
        context = payload.context
        if not self._is_grout_provider(context, router):
            return
        port = payload.metadata['port']
        subnets = payload.metadata['subnets']
        router_interface_info = self.l3plugin._make_router_interface_info(
            router.id, port['project_id'], port['id'], port['network_id'],
            subnets[-1]['id'], [subnet['id'] for subnet in subnets])
        LOG.info('Attaching port %s to grout router VM %s',
                 port['id'], router.id)
        try:
            self.vm_manager.attach_port_to_vm(
                context, router.id, port['id'],
                project_id=port.get('project_id'))
            LOG.info('Successfully attached port %s to router %s',
                     port['id'], router.id)
        except Exception as e:
            LOG.error('Failed to attach port %s to router %s: %s',
                      port['id'], router.id, e)

    @registry.receives(resources.ROUTER_INTERFACE, [events.AFTER_DELETE])
    def _process_remove_router_interface(self, resource, event, trigger,
                                         payload):
        router = payload.states[0]
        context = payload.context
        if not self._is_grout_provider(context, router):
            return
        subnet_ids = payload.metadata['subnet_ids']
        port = payload.metadata.get('port')
        if port:
            LOG.info('Detaching port %s from grout router VM %s',
                     port['id'], router.id)
            try:
                self.vm_manager.detach_port_from_vm(
                    context, router.id, port['id'])
                LOG.info('Successfully detached port %s from router %s',
                         port['id'], router.id)
            except Exception as e:
                LOG.error('Failed to detach port %s from router %s: %s',
                          port['id'], router.id, e)
        else:
            LOG.debug('No port info for interface removal on router %s, '
                      'subnets=%s', router.id, subnet_ids)

    @registry.receives(resources.FLOATING_IP, [events.AFTER_CREATE])
    def _process_floatingip_create(self, resource, event, trigger, payload):
        context = payload.context
        fip = payload.states[0]
        if not fip['router_id']:
            return
        router = self.l3plugin.get_router(context, fip['router_id'])
        if not self._is_grout_provider(context, router):
            return
        LOG.debug('Got request to create a floating ip associated to a router '
                  'of user defined flavor %s', fip)

    @registry.receives(resources.FLOATING_IP, [events.AFTER_UPDATE])
    def _process_floatingip_update(self, resource, event, trigger, payload):
        context = payload.context
        fip = payload.states[1]
        if not fip['router_id']:
            return
        router = self.l3plugin.get_router(context, fip['router_id'])
        if not self._is_grout_provider(context, router):
            return
        LOG.debug('Got request to update a floating ip associated to a router '
                  'of user defined flavor %s', fip)

    @registry.receives(resources.FLOATING_IP, [events.AFTER_DELETE])
    def _process_floatingip_delete(self, resource, event, trigger, payload):
        context = payload.context
        fip = payload.states[0]
        if not fip['router_id']:
            return
        router = self.l3plugin.get_router(context, fip['router_id'])
        if not self._is_grout_provider(context, router):
            return
        LOG.debug('Got request to delete a floating ip associated to a router '
                  'of user defined flavor %s', fip)

    @registry.receives(resources.FLOATING_IP, [events.AFTER_STATUS_UPDATE])
    def _process_floatingip_status_update(self, resource, event, trigger,
                                          payload):
        context = payload.context
        fip = payload.states[0]
        if not fip['router_id']:
            return
        router = self.l3plugin.get_router(context, fip['router_id'])
        if not self._is_grout_provider(context, router):
            return
        LOG.debug('Got request to update the status of a floating ip '
                  'associated to a router of user defined flavor %s', fip)


@registry.has_registry_receivers
class GroutNoLsp(Grout):

    @registry.receives(resources.ROUTER_INTERFACE, [events.AFTER_CREATE])
    def _process_add_router_interface(self, resource, event, trigger, payload):
        router = payload.states[0]
        context = payload.context
        if not self._is_grout_provider(context, router):
            return
        port = payload.metadata['port']
        subnets = payload.metadata['subnets']
        router_interface_info = self.l3plugin._make_router_interface_info(
            router.id, port['project_id'], port['id'], port['network_id'],
            subnets[-1]['id'], [subnet['id'] for subnet in subnets])
        self.l3plugin._ovn_client.delete_port(context, port['id'],
                                              port_object=port)
        LOG.debug('Got request to add interface %s to a user defined flavor '
                  'router with id %s. The OVN LSP was deleted.',
                  router_interface_info, router.id)

    def _get_port_to_recreate(self, context, router_id, subnet_id):
        with db_api.CONTEXT_READER.using(context):
            objs = router.RouterPort.get_objects(
                context, router_id=router_id,
                port_type=const.DEVICE_OWNER_ROUTER_INTF)
            router_ports = [
                self.l3plugin._plugin._make_port_dict(rp.db_obj.port)
                for rp in objs]
        for rp in router_ports:
            for ip in rp['fixed_ips']:
                if ip['subnet_id'] == subnet_id:
                    return rp
        raise l3_exc.RouterInterfaceNotFoundForSubnet(router_ports=router_id,
                                                      subnet_id=subnet_id)

    @registry.receives(resources.ROUTER_INTERFACE, [events.BEFORE_DELETE])
    def _process_before_remove_router_interface(self, resource, event, trigger,
                                                payload):
        context = payload.context
        router_id = payload.resource_id
        router_obj = router.Router.get_object(context, id=router_id)
        if not self._is_grout_provider(context, router_obj):
            return
        subnet_id = payload.metadata['subnet_id']
        port = self._get_port_to_recreate(context, router_id, subnet_id)

        nbdb_idl = self.l3plugin._ovn_client._nb_idl
        if nbdb_idl.lookup('Logical_Switch_Port', port['id'], default=None):
            return

        self.l3plugin._ovn_client.create_port(context, port)
        LOG.debug('Recreated OVN LSP for port with id %s before removing '
                  'interface for user defined flavor router with id %s',
                  port['id'], router_id)
