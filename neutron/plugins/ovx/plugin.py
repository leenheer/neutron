# Copyright 2014 Open Networking Laboratory
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

"""
Neutron Plug-in for OpenVirteX Network Virtualization Platform.
This plugin will forward authenticated REST API calls to OVX.
"""

import sys
import uuid
import time

from oslo.config import cfg

from neutron import context as ctx
from neutron.common import constants as q_const
from neutron.common import rpc as q_rpc
from neutron.common import topics
from neutron.db import agents_db
from neutron.db import db_base_plugin_v2
from neutron.db import dhcp_rpc_base
from neutron.db import portbindings_db
from neutron.db import quota_db  # noqa
from neutron.extensions import portbindings
from neutron.openstack.common import log as logging
from neutron.openstack.common import rpc
from neutron.plugins.common import constants as svc_constants
from neutron.plugins.ovx import ovxlib
from neutron.plugins.ovx import ovxdb
from neutron.plugins.ovx.common import config
from novaclient.v1_1.client import Client as nova_client

LOG = logging.getLogger(__name__)

class OVXRpcCallbacks(dhcp_rpc_base.DhcpRpcCallbackMixin):

    RPC_API_VERSION = '1.1'

    def __init__(self, plugin):
        self.plugin = plugin

    def create_rpc_dispatcher(self):
        '''Get the rpc dispatcher for this manager.

        If a manager would like to set an rpc API version, or support more than
        one class as the target of rpc messages, override this method.
        '''
        return q_rpc.PluginRpcDispatcher([self, agents_db.AgentExtRpcCallback()])

    def update_ports(self, rpc_context, **kwargs):
        LOG.debug(_("Agent has port updates"))
        port_id = kwargs.get('port_id')
        dpid = kwargs.get('dpid')
        port_number = kwargs.get('port_number')

        with rpc_context.session.begin(subtransactions=True):
            # Lookup port
            port_db = self.plugin.get_port(rpc_context, port_id)

            # Lookup OVX tenant ID
            neutron_network_id = port_db['network_id']
            ovx_tenant_id = ovxdb.get_ovx_tenant_id(rpc_context.session,
                                                    neutron_network_id)

            # Create OVX port
            (ovx_vdpid, ovx_vport) = self.plugin.ovx_client.createPort(ovx_tenant_id, ovxlib.hexToLong(dpid), int(port_number))

            # Stop port if requested (port is started by default in OVX)
            if not port_db['admin_state_up']:
                self.plugin.ovx_client.stopPort(ovx_tenant_id, ovx_vdpid, ovx_vport)

            # Save mapping between Neutron port ID and OVX dpid and port number
            ovxdb.add_ovx_vport(rpc_context.session, port_db['id'], ovx_vdpid, ovx_vport)

            # Register host in OVX
            self.plugin.ovx_client.connectHost(ovx_tenant_id, ovx_vdpid, ovx_vport, port_db['mac_address'])

            # Set port in active state in db
            ovxdb.set_port_status(rpc_context.session, port_db['id'], q_const.PORT_STATUS_ACTIVE)

class ControllerManager():
    """Simple manager for SDN controllers. Spawns a VM running a controller for each request
    inside the control network."""
    def __init__(self, ctrl_network):
        self.ctrl_network_id = ctrl_network['id']
        self.ctrl_network_name = ctrl_network['name']
        # Nova config for default controllers
        self._nova = nova_client(username=cfg.CONF.NOVA.username, api_key=cfg.CONF.NOVA.password,
                                project_id=cfg.CONF.NOVA.project_id, auth_url=cfg.CONF.NOVA.auth_url,
                                service_type="compute")
        try:
            self._image = self._nova.images.find(name=cfg.CONF.NOVA.image_name)
            self._flavor = self._nova.flavors.find(name=cfg.CONF.NOVA.flavor)
        except Exception as e:
            LOG.error("Could not initialize Nova bindings. Check your config.")
            sys.exit(1)

    def spawn(self, name):
        """Spawns SDN controller inside the control network.
        Returns the Nova server ID and IP address."""
        nic_config = {'net-id': self.ctrl_network_id}
        # Can also set 'fixed_ip' if needed
        server = self._nova.servers.create(name='OVX-%s' % name,
                                           image=self._image,
                                           flavor=self._flavor,
                                           nics=[nic_config])
        controller_id = server.id
        # TODO: need a good way to obtain IP address
        while self.ctrl_network_name not in self._nova.servers.find(id=controller_id).addresses:
            LOG.error('WAITING %s' % server.addresses)
            time.sleep(1)
        controller_ip = server.addresses[self.ctrl_network_name][0]['addr']
        LOG.info("Spawned SDN controller ID %s and IP %s" %  (controller_id, controller_ip))
        return (controller_id, controller_ip)

    def delete(self, controller_id):
        self._nova.servers.find(id=controller_id).delete()
                    
class OVXNeutronPlugin(db_base_plugin_v2.NeutronDbPluginV2,
                       agents_db.AgentDbMixin,
                       portbindings_db.PortBindingMixin):

    supported_extension_aliases = ['quotas', 'binding', 'agent']

    def __init__(self):
        super(OVXNeutronPlugin, self).__init__()
        # Initialize OVX client API
        self.conf_ovx = cfg.CONF.OVX
        self.ovx_client = ovxlib.OVXClient(self.conf_ovx.api_host, self.conf_ovx.api_port,
                                           self.conf_ovx.username, self.conf_ovx.password)
        # Init port bindings
        self.base_binding_dict = {
            portbindings.VIF_TYPE: portbindings.VIF_TYPE_OVS
        }
        #portbindings_db.register_port_dict_function()
        # Init RPC
        self.setup_rpc()
        # Create empty control network
        self.ctrl_network = self._setup_ctrl_network()
        # Controller manager
        self.ctrl_manager = ControllerManager(self.ctrl_network)

    def setup_rpc(self):
        # RPC support
        self.service_topics = {svc_constants.CORE: topics.PLUGIN}
        self.conn = rpc.create_connection(new=True)
        self.callbacks = OVXRpcCallbacks(self)
        self.dispatcher = self.callbacks.create_rpc_dispatcher()
        for svc_topic in self.service_topics.values():
            self.conn.create_consumer(svc_topic, self.dispatcher, fanout=False)
        # Consume from all consumers in a thread
        self.conn.consume_in_thread()

    def create_network(self, context, network):
        """Creates an OVX-based virtual network.

        The virtual network is a big switch composed out of all physical switches (this
        includes both software and hardware switches) that are connected to OVX.
        An image that is running an OpenFlow controller is spawned for the virtual network.
        """
        LOG.debug("Neutron OVX: create network")
        with context.session.begin(subtransactions=True):
            # Save in db
            net_db = super(OVXNeutronPlugin, self).create_network(context, network)

            (controller_id, controller_ip) = self.ctrl_manager.spawn(net_db['id'])
            
            ctrl = 'tcp:%s:%s' % (controller_ip, cfg.CONF.NOVA.image_port)
            # Subnet value is irrelevant to OVX
            subnet = '10.0.0.0/24'
            
            ovx_tenant_id = self._do_big_switch_network(ctrl, subnet)
            # Start network if requested
            if net_db['admin_state_up']:
                self.ovx_client.startNetwork(ovx_tenant_id)

            # Save mapping between Neutron network ID and OVX tenant ID
            ovxdb.add_ovx_network(context.session, net_db['id'], ovx_tenant_id, controller_id)

        # Return created network
        return net_db

    def update_network(self, context, id, network):
        """Update values of a network.

        :param context: neutron api request context
        :param id: UUID representing the network to update.
        :param network: dictionary with keys indicating fields to update.
                        valid keys are those that have a value of True for
                        'allow_put' as listed in the
                        :obj:`RESOURCE_ATTRIBUTE_MAP` object in
                        :file:`neutron/api/v2/attributes.py`.
        """
        LOG.debug("Neutron OVX: update network")
        # requested admin state
        req_state = network['network']['admin_state_up']
        # lookup old network state
        net_db = super(OVXNeutronPlugin, self).get_network(context, id)
        db_state = net_db['admin_state_up']
        # Start or stop network as needed
        if req_state != db_state:
            ovx_tenant_id = ovxdb.get_ovx_tenant_id(context.session, id)
            if req_state:
                self.ovx_client.startNetwork(ovx_tenant_id)
            else:
                self.ovx_client.stopNetwork(ovx_tenant_id)

        # Save network to db
        return super(OVXNeutronPlugin, self).update_network(context, id, network)

    def delete_network(self, context, id):
        """Delete a network.

        :param context: neutron api request context
        :param id: UUID representing the network to delete.
        """
        LOG.debug("Neutron OVX: delete network")
        with context.session.begin(subtransactions=True):
            # Lookup OVX tenant ID
            ovx_tenant_id = ovxdb.get_ovx_tenant_id(context.session, id)
            self.ovx_client.removeNetwork(ovx_tenant_id)

            # Lookup server ID of OpenFlow controller
            ovx_controller = ovxdb.get_ovx_controller(context.session, id)
            #self.ctrl_manager.delete(ovx_controller)

            # Remove network from db
            super(OVXNeutronPlugin, self).delete_network(context, id)

    def create_port(self, context, port):
        """Create a port.

        Create a port, which is a connection point of a device (e.g., a VM
        NIC) to attach to a L2 neutron network.

        :param context: neutron api request context
        :param port: dictionary describing the port, with keys as listed in the
                     :obj:`RESOURCE_ATTRIBUTE_MAP` object in
                     :file:`neutron/api/v2/attributes.py`.  All keys will be
                     populated.
        """
        LOG.debug("Neutron OVX: create port")
        
        #self._check_valid_port(port['port'])
        
        with context.session.begin(subtransactions=True):
            # Set port status as 'DOWN' - will be updated by agent RPC
            port['port']['status'] = q_const.PORT_STATUS_DOWN
            
            # Plugin DB - Port Create and Return port
            neutron_port = super(OVXNeutronPlugin, self).create_port(context, port)

            if port['port']['network_id'] == self.ctrl_network['id']:
                LOG.debug("Setting port binding to ctrl for port %s" % port['port'])
                self.base_binding_dict[portbindings.BRIDGE] = cfg.CONF.OVS.ctrl_bridge
            else:
                LOG.debug("Setting port binding to data for port %s" % port['port'])
                self.base_binding_dict[portbindings.BRIDGE] = cfg.CONF.OVS.data_bridge

            self._process_portbindings_create_and_update(context, port['port'], neutron_port)

            # Can't create the port in OVX yet, we need the dpid & port
            # Wait for agent to tell us
            
        # Plugin DB - Port Create and Return port
        return neutron_port

    def update_port(self, context, id, port):
        """Update values of a port.

        :param context: neutron api request context
        :param id: UUID representing the port to update.
        :param port: dictionary with keys indicating fields to update.
                     valid keys are those that have a value of True for
                     'allow_put' as listed in the :obj:`RESOURCE_ATTRIBUTE_MAP`
                     object in :file:`neutron/api/v2/attributes.py`.
        """
        LOG.debug("Neutron OVX: update port")
        
        #self._check_valid_port(port['port'])
        
        # TODO: log error when trying to change network_id or mac_address
        # requested admin state
        req_state = port['port'].get('admin_state_up')
        # lookup old port state
        port_db = super(OVXNeutronPlugin, self).get_port(context, id)
        db_state = port_db['admin_state_up']
        # Start or stop port as needed
        if (req_state != None) and (req_state != db_state):
            ovx_tenant_id = ovxdb.get_ovx_tenant_id(context.session, port_db['network_id'])
            (ovx_vdpid, ovx_vport) = ovxdb.get_ovx_vport(context.session, id)
            if req_state:
                self.ovx_client.startPort(ovx_tenant_id, ovx_vdpid, ovx_vport)
            else:
                self.ovx_client.stopPort(ovx_tenant_id, ovx_vdpid, ovx_vport)

        # Save port to db
        neutron_port = super(OVXNeutronPlugin, self).update_port(context, id, port)

        self._process_portbindings_create_and_update(context, port['port'], neutron_port)

        return neutron_port
    
    def delete_port(self, context, id):
        """Delete a port.

        :param context: neutron api request context
        :param id: UUID representing the port to delete.
        """
        LOG.debug("Neutron OVX: delete port")
        
        #self._check_valid_port(port['port'])
        
        with context.session.begin(subtransactions=True):
            # Lookup OVX tenant ID, virtual dpid and virtual port number
            neutron_network_id = super(OVXNeutronPlugin, self).get_port(context, id)['network_id']
            ovx_tenant_id = ovxdb.get_ovx_tenant_id(context.session, neutron_network_id)
            (ovx_vdpid, ovx_vport) = ovxdb.get_ovx_vport(context.session, id)
            # If OVX throws an exception, assume the virtual port was already gone in OVX
            # as the physical port removal (by nova) triggers the virtual port removal.
            # Any other exception (e.g., OVX is down) will lead to failure of this method.
            # A better way of handling this is by having the agent signal the removal of the port.
            # Not sure if this solution works when nova deletes a vm though.
            try:
                self.ovx_client.removePort(ovx_tenant_id, ovx_vdpid, ovx_vport)
            except ovxlib.OVXException:
                LOG.warn("Could not remove port. Probably because physical port was already removed.")

            # Remove network from db
            super(OVXNeutronPlugin, self).delete_port(context, id)

    def _do_big_switch_network(self, ctrl, subnet, routing='spf', num_backup=1):
        """Create virtual network in OVX that is a single big switch.

        If any step fails during network creation, no virtual network will be created."""

        if isinstance(ctrl, list):
            ctrls = ctrl
        else:
            ctrls = [ctrl]

        # Split subnet in network address and netmask
        (net_address, net_mask) = subnet.split('/')

        # Request physical topology and create virtual network
        phy_topo = self.ovx_client.getPhysicalTopology()
        tenant_id = self.ovx_client.createNetwork(ctrls, net_address, int(net_mask))

        # Fail if there are no physical switches
        switches = phy_topo.get('switches')
        if switches == None:
            raise Exception("Cannot create virtual network without physical switches")

        # Create big switch, remove virtual network if something went wrong
        try:
            # Create virtual switch with all physical dpids
            dpids = [ovxlib.hexToLong(dpid) for dpid in switches]
            vdpid = self.ovx_client.createSwitch(tenant_id, dpids)
            # Set routing algorithm and number of backups
            if (len(dpids) > 1):
                self.ovx_client.setInternalRouting(tenant_id, vdpid, routing, num_backup)
        except Exception:
            self.ovx_client.removeNetwork(tenant_id)
            raise

        return tenant_id

    def _setup_ctrl_network(self):
        """Creates network in Neutron, return network dict."""
        LOG.debug("Setting up control network")
        context = ctx.get_admin_context()
        # TODO: add tenant_id? (lookup by project_id)
        network = {
            'network': {
                'name': 'OVX_ctrl_network',
                'admin_state_up': True,
                'shared': False
                }
        }
        subnet = {
            'subnet': {
                'name': 'OVX_ctrl_subnet',
                'ip_version': 4,
                'cidr': '192.168.0.0/24',
                'gateway_ip': None,
                'dns_nameservers': [],
                'allocation_pools': [{'start': '192.168.0.1', 'end': '192.168.0.254'}],
                'host_routes': [],
                'enable_dhcp': True
            }
        }

        with context.session.begin(subtransactions=True):
            # Register network and subnet in db
            net = super(OVXNeutronPlugin, self).create_network(context, network)
            subnet['subnet']['network_id'] = net['id']
            subnet = super(OVXNeutronPlugin, self).create_subnet(context, subnet)

        return net

    def _check_valid_port(self, port):
        """Check if port is valid. Raise exception if port is being created on the controller network."""
        if (port['network_id'] == self.ctrl_network['id']):
            raise Exception("Port operation in controller network not allowed (probably DHCP agent)")
