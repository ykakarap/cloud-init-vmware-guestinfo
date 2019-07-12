# Cloud-Init Datasource for VMware Guestinfo
#
# Copyright (c) 2018 VMware, Inc. All Rights Reserved.
#
# This product is licensed to you under the Apache 2.0 license (the "License").
# You may not use this product except in compliance with the Apache 2.0 License.
#
# This product may include a number of subcomponents with separate copyright
# notices and license terms. Your use of these subcomponents is subject to the
# terms and conditions of the subcomponent's license, as noted in the LICENSE
# file.
#
# Authors: Anish Swaminathan <anishs@vmware.com>
#          Andrew Kutz <akutz@vmware.com>
#

'''
A cloud init datasource for VMware GuestInfo.
'''

import base64
import collections
import copy
from distutils.spawn import find_executable
import json
import socket
import zlib

from cloudinit import log as logging
from cloudinit import sources
from cloudinit import util
from cloudinit import safeyaml

from deepmerge import always_merger
import netifaces

LOG = logging.getLogger(__name__)
NOVAL = "No value found"
VMTOOLSD = find_executable("vmtoolsd")


class NetworkConfigError(Exception):
    '''
    NetworkConfigError is raised when there is an issue getting or
    applying network configuration.
    '''
    pass


class DataSourceVMwareGuestInfo(sources.DataSource):
    '''
    This cloud-init datasource was designed for use with CentOS 7,
    which uses cloud-init 0.7.9. However, this datasource should
    work with any Linux distribution for which cloud-init is
    avaialble.

    The documentation for cloud-init 0.7.9's datasource is
    available at http://bit.ly/cloudinit-datasource-0-7-9. The
    current documentation for cloud-init is found at
    https://cloudinit.readthedocs.io/en/latest/.

    Setting the hostname:
        The hostname is set by way of the metadata key "local-hostname".

    Setting the instance ID:
        The instance ID may be set by way of the metadata key "instance-id".
        However, if this value is absent then then the instance ID is
        read from the file /sys/class/dmi/id/product_uuid.

    Configuring the network:
        The network is configured by setting the metadata key "network"
        with a value consistent with Network Config Versions 1 or 2,
        depending on the Linux distro's version of cloud-init:

            Network Config Version 1 - http://bit.ly/cloudinit-net-conf-v1
            Network Config Version 2 - http://bit.ly/cloudinit-net-conf-v2

        For example, CentOS 7's official cloud-init package is version
        0.7.9 and does not support Network Config Version 2. However,
        this datasource still supports supplying Network Config Version 2
        data as long as the Linux distro's cloud-init package is new
        enough to parse the data.

        The metadata key "network.encoding" may be used to indicate the
        format of the metadata key "network". Valid encodings are base64
        and gzip+base64.
    '''

    dsname = 'VMwareGuestInfo'

    def __init__(self, sys_cfg, distro, paths, ud_proc=None):
        sources.DataSource.__init__(self, sys_cfg, distro, paths, ud_proc)
        if not VMTOOLSD:
            LOG.error("Failed to find vmtoolsd")

    def get_data(self):
        """
        This method should really be _get_data in accordance with the most
        recent versions of cloud-init. However, because the datasource
        supports as far back as cloud-init 0.7.9, get_data is still used.

        Because of this the method attempts to do some of the same things
        that the get_data functions in newer versions of cloud-init do,
        such as calling persist_instance_data.
        """
        if not VMTOOLSD:
            LOG.error("vmtoolsd is required to fetch guestinfo value")
            return False

        # Get the metadata.
        self.metadata = load_metadata()

        # Get the user data.
        self.userdata_raw = guestinfo('userdata')

        # Get the vendor data.
        self.vendordata_raw = guestinfo('vendordata')

        return True

    def setup(self, is_new_instance):
        """setup(is_new_instance)

        This is called before user-data and vendor-data have been processed.

        Unless the datasource has set mode to 'local', then networking
        per 'fallback' or per 'network_config' will have been written and
        brought up the OS at this point.
        """

        # Get information about the host.
        host_info = get_host_info()
        LOG.info("got host-info: %s", host_info)

        # Ensure the metadata gets updated with information about the
        # host, including the network interfaces, default IP addresses,
        # etc.
        self.metadata = merge_meta_host_data(self.metadata, host_info)

        # Persist the instance data for versions of cloud-init that support
        # doing so. This occurs here rather than in the get_data call in
        # order to ensure that the network interfaces are up and can be
        # persisted with the metadata.
        try:
            self.persist_instance_data()
        except AttributeError:
            pass

    @property
    def network_config(self):
        if 'network' in self.metadata:
            LOG.debug("using metadata network config")
        else:
            LOG.debug("using fallback network config")
            self.metadata['network'] = {
                'config': self.distro.generate_fallback_config(),
            }
        return self.metadata['network']['config']

    def get_instance_id(self):
        # Pull the instance ID out of the metadata if present. Otherwise
        # read the file /sys/class/dmi/id/product_uuid for the instance ID.
        if self.metadata and 'instance-id' in self.metadata:
            return self.metadata['instance-id']
        with open('/sys/class/dmi/id/product_uuid', 'r') as id_file:
            self.metadata['instance-id'] = str(id_file.read()).rstrip()
            return self.metadata['instance-id']


def decode(key, enc_type, data):
    '''
    decode returns the decoded string value of data
    key is a string used to identify the data being decoded in log messages
    ----
    In py 2.7:
    json.loads method takes string as input
    zlib.decompress takes and returns a string
    base64.b64decode takes and returns a string
    -----
    In py 3.6 and newer:
    json.loads method takes bytes or string as input
    zlib.decompress takes and returns a bytes
    base64.b64decode takes bytes or string and returns bytes
    -----
    In py > 3, < 3.6:
    json.loads method takes string as input
    zlib.decompress takes and returns a bytes
    base64.b64decode takes bytes or string and returns bytes
    -----
    Given the above conditions the output from zlib.decompress and
    base64.b64decode would be bytes with newer python and str in older
    version. Thus we would covert the output to str before returning
    '''
    LOG.debug("Getting encoded data for key=%s, enc=%s", key, enc_type)

    raw_data = None
    if enc_type == "gzip+base64" or enc_type == "gz+b64":
        LOG.debug("Decoding %s format %s", enc_type, key)
        raw_data = zlib.decompress(base64.b64decode(data), zlib.MAX_WBITS | 16)
    elif enc_type == "base64" or enc_type == "b64":
        LOG.debug("Decoding %s format %s", enc_type, key)
        raw_data = base64.b64decode(data)
    else:
        LOG.debug("Plain-text data %s", key)
        raw_data = data

    if isinstance(raw_data, bytes):
        return raw_data.decode('utf-8')
    return raw_data


def get_guestinfo_value(key):
    '''
    Returns a guestinfo value for the specified key.
    '''
    LOG.debug("Getting guestinfo value for key %s", key)
    try:
        (stdout, stderr) = util.subp(
            [VMTOOLSD, "--cmd", "info-get guestinfo." + key])
        if stderr == NOVAL:
            LOG.debug("No value found for key %s", key)
        elif not stdout:
            LOG.error("Failed to get guestinfo value for key %s", key)
        else:
            return stdout.rstrip()
    except util.ProcessExecutionError as error:
        if error.stderr == NOVAL:
            LOG.debug("No value found for key %s", key)
        else:
            util.logexc(
                LOG, "Failed to get guestinfo value for key %s: %s", key, error)
    except Exception:
        util.logexc(
            LOG, "Unexpected error while trying to get guestinfo value for key %s", key)
    return None


def guestinfo(key):
    '''
    guestinfo returns the guestinfo value for the provided key, decoding
    the value when required
    '''
    data = get_guestinfo_value(key)
    if not data:
        return None
    enc_type = get_guestinfo_value(key + '.encoding')
    return decode('guestinfo.' + key, enc_type, data)


def load(data):
    '''
    load first attempts to unmarshal the provided data as JSON, and if
    that fails then attempts to unmarshal the data as YAML. If data is
    None then a new dictionary is returned.
    '''
    if not data:
        return {}
    try:
        return json.loads(data)
    except:
        return safeyaml.load(data)


def load_metadata():
    '''
    load_metadata loads the metadata from the guestinfo data, optionally
    decoding the network config when required
    '''
    data = load(guestinfo('metadata'))
    LOG.debug('loaded metadata %s', data)

    network = None
    if 'network' in data:
        network = data['network']
        del data['network']

    network_enc = None
    if 'network.encoding' in data:
        network_enc = data['network.encoding']
        del data['network.encoding']

    if network:
        LOG.debug('network data found')
        if isinstance(network, collections.Mapping):
            LOG.debug("network data copied to 'config' key")
            network = {
                'config': copy.deepcopy(network)
            }
        else:
            LOG.debug("network data to be decoded %s", network)
            dec_net = decode('metadata.network', network_enc, network)
            network = {
                'config': load(dec_net),
            }

        LOG.debug('network data %s', network)
        data['network'] = network

    return data


def get_datasource_list(depends):
    '''
    Return a list of data sources that match this set of dependencies
    '''
    return [DataSourceVMwareGuestInfo]


def get_default_ip_addrs():
    '''
    Returns the default IPv4 and IPv6 addresses based on the device(s) used for
    the default route. Please note that None may be returned for either address
    family if that family has no default route or if there are multiple
    addresses associated with the device used by the default route for a given
    address.
    '''
    gateways = netifaces.gateways()
    if 'default' not in gateways:
        return None, None

    default_gw = gateways['default']
    if netifaces.AF_INET not in default_gw and netifaces.AF_INET6 not in default_gw:
        return None, None

    ipv4 = None
    ipv6 = None

    gw4 = default_gw.get(netifaces.AF_INET)
    if gw4:
        _, dev4 = gw4
        addr4_fams = netifaces.ifaddresses(dev4)
        if addr4_fams:
            af_inet4 = addr4_fams.get(netifaces.AF_INET)
            if af_inet4:
                if len(af_inet4) > 1:
                    LOG.warn(
                        "device %s has more than one ipv4 address: %s", dev4, af_inet4)
                elif 'addr' in af_inet4[0]:
                    ipv4 = af_inet4[0]['addr']

    # Try to get the default IPv6 address by first seeing if there is a default
    # IPv6 route.
    gw6 = default_gw.get(netifaces.AF_INET6)
    if gw6:
        _, dev6 = gw6
        addr6_fams = netifaces.ifaddresses(dev6)
        if addr6_fams:
            af_inet6 = addr6_fams.get(netifaces.AF_INET6)
            if af_inet6:
                if len(af_inet6) > 1:
                    LOG.warn(
                        "device %s has more than one ipv6 address: %s", dev6, af_inet6)
                elif 'addr' in af_inet6[0]:
                    ipv6 = af_inet6[0]['addr']

    # If there is a default IPv4 address but not IPv6, then see if there is a
    # single IPv6 address associated with the same device associated with the
    # default IPv4 address.
    if ipv4 and not ipv6:
        af_inet6 = addr4_fams.get(netifaces.AF_INET6)
        if af_inet6:
            if len(af_inet6) > 1:
                LOG.warn(
                    "device %s has more than one ipv6 address: %s", dev4, af_inet6)
            elif 'addr' in af_inet6[0]:
                ipv6 = af_inet6[0]['addr']

    # If there is a default IPv6 address but not IPv4, then see if there is a
    # single IPv4 address associated with the same device associated with the
    # default IPv6 address.
    if not ipv4 and ipv6:
        af_inet4 = addr6_fams.get(netifaces.AF_INET4)
        if af_inet4:
            if len(af_inet4) > 1:
                LOG.warn(
                    "device %s has more than one ipv4 address: %s", dev6, af_inet4)
            elif 'addr' in af_inet4[0]:
                ipv4 = af_inet4[0]['addr']

    return ipv4, ipv6


def get_host_info():
    '''
    Returns host information such as the host name and network interfaces.
    '''

    host_info = {
        'network': {
            'interfaces': {
                'by-mac': collections.OrderedDict(),
                'by-ipv4': collections.OrderedDict(),
                'by-ipv6': collections.OrderedDict(),
            },
        },
    }

    hostname = socket.getfqdn()
    if hostname:
        host_info['hostname'] = hostname
        host_info['local-hostname'] = hostname

    default_ipv4, default_ipv6 = get_default_ip_addrs()
    if default_ipv4:
        host_info['local-ipv4'] = default_ipv4
    if default_ipv6:
        host_info['local-ipv6'] = default_ipv6

    by_mac = host_info['network']['interfaces']['by-mac']
    by_ipv4 = host_info['network']['interfaces']['by-ipv4']
    by_ipv6 = host_info['network']['interfaces']['by-ipv6']

    ifaces = netifaces.interfaces()
    for dev_name in ifaces:
        addr_fams = netifaces.ifaddresses(dev_name)
        af_link = addr_fams.get(netifaces.AF_LINK)
        af_inet4 = addr_fams.get(netifaces.AF_INET)
        af_inet6 = addr_fams.get(netifaces.AF_INET6)

        mac = None
        if af_link and 'addr' in af_link[0]:
            mac = af_link[0]['addr']

        # Do not bother recording localhost
        if mac == "00:00:00:00:00:00":
            continue

        if mac and (af_inet4 or af_inet6):
            key = mac
            val = {}
            if af_inet4:
                val["ipv4"] = af_inet4
            if af_inet6:
                val["ipv6"] = af_inet6
            by_mac[key] = val

        if af_inet4:
            for ip_info in af_inet4:
                key = ip_info['addr']
                if key == '127.0.0.1':
                    continue
                val = copy.deepcopy(ip_info)
                del val['addr']
                if mac:
                    val['mac'] = mac
                by_ipv4[key] = val

        if af_inet6:
            for ip_info in af_inet6:
                key = ip_info['addr']
                if key == '::1':
                    continue
                val = copy.deepcopy(ip_info)
                del val['addr']
                if mac:
                    val['mac'] = mac
                by_ipv6[key] = val

    return host_info


def merge_meta_host_data(metadata, host_info):
    # Combine host_info and metadata.
    # Values in metada should be preserved as provided by the user
    res = always_merger.merge(host_info, metadata)

    # Make sure that the 'local-hostname' and 'hostname' are in sync.
    # If the user provided 'local-hostname' override 'hostname' with 
    # that value
    res['hostname'] = res['local-hostname']


def main():
    '''
    Executed when this file is used as a program.
    '''
    metadata = {'network': {'config': {'dhcp': True}}}
    host_info = get_host_info()
    metadata = merge_meta_host_data(metadata, host_info)
    print(util.json_dumps(metadata))


if __name__ == "__main__":
    main()

# vi: ts=4 expandtab
