# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import datetime
import functools

from oslo_utils import timeutils

from nova import test
from nova.tests import fixtures as nova_fixtures
from nova.tests.functional import fixtures as func_fixtures
from nova.tests.functional import integrated_helpers
from nova.tests.unit.image import fake as fake_image
from nova.tests.unit import policy_fixture
from nova import utils


class HostStatusPolicyTestCase(test.TestCase,
                               integrated_helpers.InstanceHelperMixin):
    """Tests host_status policies behavior in the API."""
    host_status_rule = 'os_compute_api:servers:show:host_status'
    host_status_unknown_only_rule = (
        'os_compute_api:servers:show:host_status:unknown-only')
    image_uuid = '155d900f-4e14-4e4c-a73d-069cbf4541e6'

    def setUp(self):
        super(HostStatusPolicyTestCase, self).setUp()
        # Setup the standard fixtures.
        fake_image.stub_out_image_service(self)
        self.addCleanup(fake_image.FakeImageService_reset)
        self.useFixture(nova_fixtures.NeutronFixture(self))
        self.useFixture(func_fixtures.PlacementFixture())
        self.useFixture(policy_fixture.RealPolicyFixture())

        # Start the services.
        self.start_service('conductor')
        self.start_service('scheduler')
        self.compute = self.start_service('compute')
        api_fixture = self.useFixture(nova_fixtures.OSAPIFixture(
            api_version='v2.1'))
        self.api = api_fixture.api
        self.admin_api = api_fixture.admin_api
        # The host_status field is returned starting in microversion 2.16.
        self.api.microversion = '2.16'
        self.admin_api.microversion = '2.16'

    def _setup_host_status_unknown_only_test(self, networks=None):
        # Set policy such that admin are allowed to see any/all host status and
        # all users are allowed to see UNKNOWN host status only.
        self.policy.set_rules({
            self.host_status_rule: 'rule:admin_api',
            self.host_status_unknown_only_rule: '@'},
            # This is needed to avoid nulling out the rest of default policy.
            overwrite=False)
        # Create a server as a normal non-admin user.
        # In microversion 2.36 the /images proxy API was deprecated, so
        # specifiy the image_uuid directly.
        kwargs = {'image_uuid': self.image_uuid}
        if networks:
            # Starting with microversion 2.37 the networks field is required.
            kwargs['networks'] = networks
        server = self._build_minimal_create_server_request(
            self.api, 'test_host_status_unknown_only', **kwargs)
        server = self.api.post_server({'server': server})
        server = self._wait_for_state_change(self.admin_api, server, 'ACTIVE')
        return server

    @staticmethod
    def _get_server(resp):
        # Get a server whether it's a single server or a list of one server.
        server = resp if not isinstance(resp, list) else resp[0]
        # The PUT /servers/{server_id} response has a 'server' attribute.
        if 'server' in server:
            server = server['server']
        return server

    def _set_server_state_active(self, server):
        # Needed for being able to issue multiple rebuild requests while the
        # compute service is down.
        reset_state = {'os-resetState': {'state': 'active'}}
        self.admin_api.post_server_action(server['id'], reset_state)

    def _test_host_status_unknown_only(self, admin_func, func):
        # Get server as admin.
        server = self._get_server(admin_func())
        # We need to wait for ACTIVE if this was a post rebuild server action,
        # else a subsequent rebuild request will fail with a 409 in the API.
        self._wait_for_state_change(self.admin_api, server, 'ACTIVE')
        # Verify admin can see the host status UP.
        self.assertEqual('UP', server['host_status'])
        # Get server as normal non-admin user.
        server = self._get_server(func())
        self._wait_for_state_change(self.admin_api, server, 'ACTIVE')
        # Verify non-admin do not receive the host_status field because it is
        # not UNKNOWN.
        self.assertNotIn('host_status', server)
        # Stop the compute service to trigger UNKNOWN host_status.
        self.compute.stop()
        # Advance time by 30 minutes so nova considers service as down.
        minutes_from_now = timeutils.utcnow() + datetime.timedelta(minutes=30)
        timeutils.set_time_override(override_time=minutes_from_now)
        self.addCleanup(timeutils.clear_time_override)
        # Get server as admin.
        server = self._get_server(admin_func())
        # Now that the compute service is down, the rebuild will not ever
        # complete. But we're only interested in what would be returned from
        # the API post rebuild action, so reset the state to ACTIVE to allow
        # the next rebuild request to go through without a 409 error.
        self._set_server_state_active(server)
        # Verify admin can see the host status UNKNOWN.
        self.assertEqual('UNKNOWN', server['host_status'])
        # Get server as normal non-admin user.
        server = self._get_server(func())
        self._set_server_state_active(server)
        # Verify non-admin can see the host status UNKNOWN too.
        self.assertEqual('UNKNOWN', server['host_status'])
        # Now, adjust the policy to make it so only admin are allowed to see
        # UNKNOWN host status only.
        self.policy.set_rules({
            self.host_status_unknown_only_rule: 'rule:admin_api'},
            overwrite=False)
        # Get server as normal non-admin user.
        server = self._get_server(func())
        self._set_server_state_active(server)
        # Verify non-admin do not receive the host_status field.
        self.assertNotIn('host_status', server)
        # Verify that admin will not receive ths host_status field if the
        # API microversion < 2.16.
        with utils.temporary_mutation(self.admin_api, microversion='2.15'):
            server = self._get_server(admin_func())
            self.assertNotIn('host_status', server)

    def test_get_server_host_status_unknown_only(self):
        server = self._setup_host_status_unknown_only_test()
        # GET /servers/{server_id}
        admin_func = functools.partial(self.admin_api.get_server, server['id'])
        func = functools.partial(self.api.get_server, server['id'])
        self._test_host_status_unknown_only(admin_func, func)

    def test_get_servers_detail_host_status_unknown_only(self):
        self._setup_host_status_unknown_only_test()
        # GET /servers/detail
        admin_func = functools.partial(self.admin_api.get_servers)
        func = functools.partial(self.api.get_servers)
        self._test_host_status_unknown_only(admin_func, func)

    def test_put_server_host_status_unknown_only(self):
        # The host_status field is returned from PUT /servers/{server_id}
        # starting in microversion 2.75.
        self.api.microversion = '2.75'
        self.admin_api.microversion = '2.75'
        server = self._setup_host_status_unknown_only_test(networks='none')
        # PUT /servers/{server_id}
        an_update = {'server': {'name': 'host-status-unknown-only'}}
        admin_func = functools.partial(self.admin_api.put_server, server['id'],
                                       an_update)
        func = functools.partial(self.api.put_server, server['id'], an_update)
        self._test_host_status_unknown_only(admin_func, func)

    def test_post_server_rebuild_host_status_unknown_only(self):
        # The host_status field is returned from POST
        # /servers/{server_id}/action (rebuild) starting in microversion 2.75.
        self.api.microversion = '2.75'
        self.admin_api.microversion = '2.75'
        server = self._setup_host_status_unknown_only_test(networks='none')
        # POST /servers/{server_id}/action (rebuild)
        rebuild = {'rebuild': {'imageRef': self.image_uuid}}
        admin_func = functools.partial(self.admin_api.post_server_action,
                                       server['id'], rebuild)
        func = functools.partial(self.api.post_server_action, server['id'],
                                 rebuild)
        self._test_host_status_unknown_only(admin_func, func)
