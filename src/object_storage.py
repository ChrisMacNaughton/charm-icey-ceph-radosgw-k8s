"""basic object storage library
"""

import yaml
import logging

from ops.framework import Object
from ops.framework import StoredState


logger = logging.getLogger(__name__)


class ObjectStoreProvides(Object):
    """
    Encapsulate the Provides side of the object-storage relation.
    Hook events observed:
    - relation-joined
    - relation-changed
    """

    charm = None
    _stored = StoredState()

    def __init__(self, charm, relation_name='object-storage'):
        super().__init__(charm, relation_name)

        self._stored.set_default(processed=[])
        self.charm = charm
        self.this_unit = self.model.unit
        self.relation_name = relation_name
        self.framework.observe(
            charm.on[self.relation_name].relation_joined,
            self._on_relation_changed
        )
        self.framework.observe(
            charm.on[self.relation_name].relation_changed,
            self._on_relation_changed
        )

    def _on_relation_changed(self, event):
        """Prepare relation for data from requiring side."""
        if not self.charm.ready():
            event.defer()
            return
        event.relation.data[self.model.app]['data'] = \
            yaml.safe_dump(self.charm.object_storage_credentials())
        event.relation.data[self.model.app]['_supported_versions'] = '- v1'
