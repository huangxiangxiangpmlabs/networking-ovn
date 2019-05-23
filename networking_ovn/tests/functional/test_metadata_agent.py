# Copyright 2018 Red Hat, Inc.
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

import threading

import mock
from neutron.common import utils as n_utils
from oslo_config import fixture as fixture_config
from oslo_utils import uuidutils
from ovsdbapp.backend.ovs_idl import event
from ovsdbapp import event as ovsdb_event

from networking_ovn.agent.metadata import agent
from networking_ovn.agent.metadata import ovsdb
from networking_ovn.agent.metadata import server as metadata_server
from networking_ovn.agent import stats
from networking_ovn.common import constants as ovn_const
from networking_ovn.conf.agent.metadata import config as meta
from networking_ovn.tests.functional import base


class MetadataAgentHealthEvent(event.RowEvent):
    event_name = 'MetadataAgentHealthEvent'
    ONETIME = True

    def __init__(self, chassis, sb_cfg, timeout=5):
        self.chassis = chassis
        self.sb_cfg = sb_cfg
        self.event = threading.Event()
        self.timeout = timeout
        super(MetadataAgentHealthEvent, self).__init__(
            (self.ROW_UPDATE,), 'Chassis', (('name', '=', self.chassis),))

    def matches(self, event, row, old=None):
        if not super(MetadataAgentHealthEvent, self).matches(event, row, old):
            return False
        return int(row.external_ids.get(
            ovn_const.OVN_AGENT_METADATA_SB_CFG_KEY, 0)) >= self.sb_cfg

    def run(self, event, row, old):
        self.event.set()

    def wait(self):
        return self.event.wait(self.timeout)


class TestMetadataAgent(base.TestOVNFunctionalBase):
    OVN_BRIDGE = 'br-int'

    def setUp(self):
        super(TestMetadataAgent, self).setUp()
        self.handler = ovsdb_event.RowEventHandler()
        self.sb_api.idl.notify = self.handler.notify
        # We only have OVN NB and OVN SB running for functional tests
        mock.patch.object(ovsdb, 'MetadataAgentOvsIdl').start()
        self._mock_get_ovn_br = mock.patch.object(
            agent.MetadataAgent,
            '_get_ovn_bridge',
            return_value=self.OVN_BRIDGE).start()
        self.agent = self._start_metadata_agent()

    def _start_metadata_agent(self):
        conf = self.useFixture(fixture_config.Config()).conf
        conf.register_opts(meta.SHARED_OPTS)
        conf.register_opts(meta.UNIX_DOMAIN_METADATA_PROXY_OPTS)
        conf.register_opts(meta.METADATA_PROXY_HANDLER_OPTS)
        conf.register_opts(meta.OVS_OPTS, group='ovs')
        meta.setup_privsep()

        ovn_sb_db = self.ovsdb_server_mgr.get_ovsdb_connection_path('sb')
        conf.set_override('ovn_sb_connection', ovn_sb_db, group='ovn')

        # We don't need the HA proxy server running for now
        p = mock.patch.object(metadata_server, 'UnixDomainMetadataProxy')
        p.start()
        self.addCleanup(p.stop)

        self.chassis_name = self.add_fake_chassis('ovs-host-fake')
        mock.patch.object(agent.MetadataAgent,
                          '_get_own_chassis_name',
                          return_value=self.chassis_name).start()
        agt = agent.MetadataAgent(conf)
        agt.start()
        # Metadata agent will open connections to OVS and SB databases.
        # Close connections to them when the test ends,
        self.addCleanup(agt.ovs_idl.ovsdb_connection.stop)
        self.addCleanup(agt.sb_idl.ovsdb_connection.stop)

        return agt

    def test_metadata_agent_healthcheck(self):
        chassis_row = self.sb_api.db_find(
            'Chassis', ('name', '=', self.chassis_name)).execute(
            check_error=True)[0]

        # Assert that, prior to creating a resource the metadata agent
        # didn't populate the external_ids from the Chassis
        self.assertNotIn(ovn_const.OVN_AGENT_METADATA_SB_CFG_KEY,
                         chassis_row['external_ids'])

        # Let's list the agents to force the nb_cfg to be bumped on NB
        # db, which will automatically increment the nb_cfg counter on
        # NB_Global and make ovn-controller copy it over to SB_Global. Upon
        # this event, Metadata agent will update the external_ids on its
        # Chassis row to signal that it's healthy.

        row_event = MetadataAgentHealthEvent(self.chassis_name, 1)
        self.handler.watch_event(row_event)
        self.new_list_request('agents').get_response(self.api)

        # If we do not time out waiting for the event, then we are assured
        # that the metadata agent has populated the external_ids from the
        # chassis with the nb_cfg, 1 revisions when listing the agents.
        self.assertTrue(row_event.wait())

    def test_updating_metadata_doesnt_update_controller_stats(self):
        chassis = self.sb_api.lookup('Chassis', self.chassis_name)
        self.assertNotIn(ovn_const.OVN_AGENT_METADATA_SB_CFG_KEY,
                         chassis.external_ids)
        nb_cfg = stats.AgentStats.get_stat(chassis.uuid).nb_cfg
        new_nb_cfg = nb_cfg + 1
        row_event = MetadataAgentHealthEvent(chassis.name, new_nb_cfg)
        self.handler.watch_event(row_event)
        self.sb_api.update_metadata_health_status(
            chassis.name, new_nb_cfg).execute(check_error=True)
        self.assertTrue(row_event.wait())
        self.assertEqual(stats.AgentStats.get_stat(chassis.uuid).nb_cfg,
                         nb_cfg)

    def _create_metadata_port(self, txn, lswitch_name):
        mdt_port_name = 'ovn-mdt-' + uuidutils.generate_uuid()
        txn.add(
            self.nb_api.lsp_add(
                lswitch_name,
                mdt_port_name,
                type='localport',
                addresses='AA:AA:AA:AA:AA:AA 192.168.122.123',
                external_ids={
                    ovn_const.OVN_CIDRS_EXT_ID_KEY: '192.168.122.123/24'}))

    def test_agent_resync_on_non_existing_bridge(self):
        BR_NEW = 'br-new'
        self._mock_get_ovn_br.return_value = BR_NEW
        self.agent.ovs_idl.list_br.return_value.execute.return_value = [BR_NEW]
        # The agent has initialized with br-int and above list_br doesn't
        # return it, hence the agent should trigger reconfiguration and store
        # new br-new value to its attribute.
        self.assertEqual(self.OVN_BRIDGE, self.agent.ovn_bridge)
        lswitch_name = 'ovn-' + uuidutils.generate_uuid()
        lswitchport_name = 'ovn-port-' + uuidutils.generate_uuid()

        with self.nb_api.transaction(check_error=True) as txn:
            txn.add(
                self.nb_api.ls_add(lswitch_name, True))
            txn.add(
                self.nb_api.create_lswitch_port(
                    lswitchport_name, lswitch_name, True))
            self._create_metadata_port(txn, lswitch_name)
        # Trigger PortBindingChassisEvent
        self.sb_api.lsp_bind(lswitchport_name, self.chassis_name).execute()

        exc = Exception("Agent bridge hasn't changed from %s to %s "
                        "in 10 seconds after Port_Binding event" %
                        (self.agent.ovn_bridge, BR_NEW))
        n_utils.wait_until_true(
            lambda: BR_NEW == self.agent.ovn_bridge,
            timeout=10,
            exception=exc)
