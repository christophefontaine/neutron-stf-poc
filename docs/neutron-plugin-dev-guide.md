# Neutron Plugin Development with Service Type Framework

## DevStack Installation Summary
- **DevStack Version**: 2026.2
- **Host IP**: 192.168.40.11
- **Keystone URL**: http://192.168.40.11/identity/
- **Admin User**: admin
- **Demo User**: demo  
- **Password**: secret

## Quick Access

### SSH to VM
```bash
ssh parallels@192.168.40.11
```

### Switch to stack user
```bash
sudo su - stack
```

### Source OpenStack credentials
```bash
source /opt/stack/devstack/openrc admin admin
```

## Directory Structure

- **DevStack**: `/opt/stack/devstack`
- **Neutron Code**: `/opt/stack/neutron`
- **Neutron Config**: `/etc/neutron`
- **Logs**: `/opt/stack/logs`

## Service Type Framework

### Current Service Providers
```bash
openstack network service provider list
```

### Neutron Configuration Files
- Main config: `/etc/neutron/neutron.conf`
- ML2 config: `/etc/neutron/plugins/ml2/ml2_conf.ini`
- Service providers: `/etc/neutron/neutron_vpnaas.conf` (for VPN example)

## Creating a New Plugin

### 1. Create Plugin Directory Structure
```bash
cd /opt/stack
mkdir -p my-neutron-plugin/my_neutron_plugin
cd my-neutron-plugin
```

### 2. Basic Plugin Structure
```
my-neutron-plugin/
├── setup.py
├── setup.cfg
├── my_neutron_plugin/
│   ├── __init__.py
│   ├── plugin.py
│   ├── db/
│   │   └── models.py
│   └── drivers/
│       └── my_driver.py
└── etc/
    └── neutron/
        └── service_providers/
            └── my_service.conf
```

### 3. Service Type Framework Integration

Your plugin needs to:
1. Define a service driver extending `neutron_lib.services.base.ServicePluginBase`
2. Register with service type framework in `service_providers` config
3. Implement required abstract methods

Example service provider config:
```ini
[service_providers]
service_provider = MY_SERVICE:my_provider:my_neutron_plugin.drivers.my_driver.MyDriver:default
```

### 4. Key Files to Study

Neutron Service Type Framework code:
```bash
/opt/stack/neutron/neutron/services/provider_configuration.py
/opt/stack/neutron/neutron/services/service_base.py
/opt/stack/neutron/neutron_lib/api/definitions/
```

Example plugins using Service Type Framework:
```bash
/opt/stack/neutron/neutron/services/l3_router/
/opt/stack/neutron/neutron/services/metering/
/opt/stack/neutron-vpnaas/  # If available
```

## Development Workflow

### 1. Install your plugin in development mode
```bash
cd /opt/stack/my-neutron-plugin
pip install -e .
```

### 2. Update neutron.conf
Add your service plugin to `/etc/neutron/neutron.conf`:
```ini
[DEFAULT]
service_plugins = router,my_service_plugin
```

### 3. Restart Neutron services
```bash
sudo systemctl restart devstack@neutron-api.service
sudo systemctl restart devstack@neutron-rpc-server.service
```

### 4. Check logs
```bash
journalctl -u devstack@neutron-api.service -f
tail -f /opt/stack/logs/screen/screen-neutron-api.log
```

## Useful Commands

### Check Neutron services status
```bash
sudo systemctl status devstack@neutron-api.service
openstack network agent list
```

### Test your plugin
```bash
# Source credentials
source /opt/stack/devstack/openrc admin admin

# Test OpenStack CLI
openstack network list
openstack subnet list
```

### Database access
```bash
# Connection string in /etc/neutron/neutron.conf
mysql -u root neutron
```

### Stop/Start DevStack
```bash
cd /opt/stack/devstack
./unstack.sh  # Stop services
./stack.sh    # Start services
```

## References

- Neutron API: http://192.168.40.11:9696
- DevStack docs: https://docs.openstack.org/devstack/latest/
- Neutron docs: https://docs.openstack.org/neutron/latest/
- Neutron Service Type Framework: https://docs.openstack.org/neutron/latest/contributor/internals/services_and_agents.html
