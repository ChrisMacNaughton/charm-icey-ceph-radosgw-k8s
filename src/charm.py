#!/usr/bin/env python3
# Copyright 2022 Chris MacNaughton
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

import logging
import json
from urllib.parse import urlparse

from lightkube import Client
from lightkube.resources.core_v1 import Service
import ops

from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, WaitingStatus
from charms.observability_libs.v0.kubernetes_service_patch import KubernetesServicePatch
from object_storage import ObjectStoreProvides
logger = logging.getLogger(__name__)


CEPH_CONF = """[client]

rgw backend store = dbstore
rgw config store = dbstore
debug rgw = 5
"""


class CephRadosgwK8SCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.radosgw_pebble_ready, self._on_radosgw_pebble_ready)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.create_user_action, self._on_create_user_action)
        # self.ingress = IngressPerAppRequirer(self, port=7480)
        self.object_storage = ObjectStoreProvides(self)
        self.port = 7480
        self.service_patch = KubernetesServicePatch(
            charm=self,
            service_type="LoadBalancer",
            ports=[(f"{self.app.name}", self.port)],
        )
        self._stored.set_default(
            url=None,
            ready=False,
        )

    def ready(self) -> bool:
        return self._stored.ready

    def _run_cmd(self, cmd: list[str], exception_on_error: bool = True, **kwargs) -> str:
        container = self.unit.get_container('radosgw')
        process = container.exec(cmd, **kwargs)
        try:
            stdout, _ = process.wait_output()
            # Not logging the command in case it included a password,
            # too cautious ?
            logger.debug('Command complete')
            if stdout:
                for line in stdout.splitlines():
                    logger.debug('    %s', line)
            return stdout
        except ops.pebble.ExecError as e:
            logger.error('Exited with code %d. Stderr:', e.exit_code)
            for line in e.stderr.splitlines():
                logger.error('    %s', line)
            if exception_on_error:
                raise

    def _on_radosgw_pebble_ready(self, event):
        """Define and start a workload using the Pebble API.

        TEMPLATE-TODO: change this example to suit your needs.
        You'll need to specify the right entrypoint and environment
        configuration for your specific workload. Tip: you can see the
        standard entrypoint of an existing container using docker inspect

        Learn more about Pebble layers at https://github.com/canonical/pebble
        """
        # Get a reference the container attribute on the PebbleReadyEvent
        container = event.workload
        # Define an initial Pebble layer configuration
        pebble_layer = {
            "summary": "radosgw layer",
            "description": "pebble config layer for radosgw",
            "services": {
                "radosgw": {
                    "override": "replace",
                    "summary": "radosgw",
                    "command": "/usr/bin/radosgw --no-mon-config -d --cluster ceph",
                    "startup": "enabled",
                }
            },
        }
        # Add initial Pebble config layer using the Pebble API
        container.add_layer("radosgw", pebble_layer, combine=True)
        # Autostart any services that were defined with startup: enabled
        container.autostart()
        # Learn more about statuses in the SDK docs:
        # https://juju.is/docs/sdk/constructs#heading--statuses
        self.unit.status = ActiveStatus(f'Acccess via {self.access_url}')
        self._stored.ready = True

    def object_storage_credentials(self):
        user = json.loads(self._get_or_create_user('object-store'))
        return {
            "access-key": user["keys"][0]["access_key"],
            "namespace": self.model.name,
            "port": self.port,
            "secret-key": user["keys"][0]["secret_key"],
            "secure": False,
            "service": self.model.app.name,
        }

    @property
    def access_url(self):
        return f'http://{self._external_host}:{self.port}'

    def _on_config_changed(self, _):
        """Just an example to show how to deal with changed configuration.

        TEMPLATE-TODO: change this example to suit your needs.
        If you don't need to handle config, you can remove this method,
        the hook created in __init__.py for it, the corresponding test,
        and the config.py file.

        Learn more about config at https://juju.is/docs/sdk/config
        """
        # current = self.config["thing"]
        # if current not in self._stored.things:
        #     logger.debug("found a new thing: %r", current)
        #     self._stored.things.append(current)
        pass

    def _on_create_user_action(self, event):
        """Just an example to show how to receive actions.

        TEMPLATE-TODO: change this example to suit your needs.
        If you don't need to handle actions, you can remove this method,
        the hook created in __init__.py for it, the corresponding test,
        and the actions.py file.

        Learn more about actions at https://juju.is/docs/sdk/actions
        """
        username = event.params['username']
        user_stdout = self._get_or_create_user(username)
        user = json.loads(user_stdout, object_hook=remove_underscores)
        logger.info(f'user: {user}')
        event.set_results({
            'result': 'success',
            'user': {x.replace('_', '-'): v
                     for x, v in user.items()},
        })

    def _get_or_create_user(self, username: str) -> str:
        try:
            return self._get_user(username)
        except ops.pebble.ExecError:
            return self._create_user(username)

    def _get_user(self, username: str) -> str:
        return self._run_cmd([
            'radosgw-admin',
            'user',
            'info',
            f'--uid="{username}"',
        ])

    def _create_user(self, username: str) -> str:
        return self._run_cmd([
            'radosgw-admin',
            'user',
            'create',
            f'--uid="{username}"',
            f'--display-name="{username}"',
        ])

    @property
    def _external_host(self):
        """Determine the external address for the ingress gateway.
        It will prefer the `external-hostname` config if that is set, otherwise
        it will look up the load balancer address for the ingress gateway.
        If the gateway isn't available or doesn't have a load balancer address yet,
        returns None.
        """
        if external_hostname := self.model.config.get("external_hostname"):
            return external_hostname

        return _get_loadbalancer_status(namespace=self.model.name, service_name=self.app.name)


def _get_loadbalancer_status(namespace: str, service_name: str):
    client = Client()
    traefik_service = client.get(Service, name=service_name, namespace=namespace)

    if status := traefik_service.status:
        if load_balancer_status := status.loadBalancer:
            if ingress_addresses := load_balancer_status.ingress:
                if ingress_address := ingress_addresses[0]:
                    return ingress_address.hostname or ingress_address.ip

    return None


def remove_underscores(obj):
    new_obj = obj.copy()
    for key in new_obj.keys():
        new_key = key.replace("_", "-")
        if new_key != key:
            obj[new_key] = obj[key]
            del obj[key]
    return obj


if __name__ == "__main__":
    main(CephRadosgwK8SCharm)
