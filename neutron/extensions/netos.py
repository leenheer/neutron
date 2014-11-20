from neutron.api.v2 import attributes
from neutron.plugins.common import constants

IMAGE = 'netos:image'
URL = 'netos:url'

EXTENDED_ATTRIBUTES_2_0 = {
    'networks': {
        IMAGE: {'allow_post': True, 'allow_put': False,
                'default': attr.ATTR_NOT_SPECIFIED,
                'validate': {'type:string': None},
                'is_visible': True
        },
        URL: {'allow_post': True, 'allow_put': False,
              'default': attr.ATTR_NOT_SPECIFIED,
              'validate': {'type:string': None},
              'is_visible': True
        }
    }
}

class NetOS(object):
    @classmethod
    def get_name(cls):
        return "Network OS Extension"

    @classmethod
    def get_alias(cls):
        return "netos"

    @classmethod
    def get_description(cls):
        return "Configure network operating system"

    @classmethod
    def get_namespace(cls):
        # return "http://docs.openstack.org/ext/provider/api/v1.0"
        # Nothing there right now
        return "http://www.vicci.org/ext/opencloud/topology/api/v0.1"

    @classmethod
    def get_updated(cls):
        return "2014-11-19T10:00:00-00:00"

    def get_extended_resources(self, version):
        if version == "2.0":
            return EXTENDED_ATTRIBUTES_2_0
        else:
            return {}
