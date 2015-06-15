from copy import deepcopy
import containers.common
from containers.common import *
from containers.vms import *
import logging
import yaml


class TopologyReader(object):
    vm_types = {"csr1000": CSR1000,
                "vyatta5600": Vyatta5600}

    def __init__(self, config_path, ifaces_naming=None):
        self.config = yaml.load(open(config_path).read())
        self.reserved_sections = 'ftp,esx,esx_vcenter,settings,VM,NET'
        for key in self.reserved_sections.split(','):
            assert key in self.config, \
                "{} block is absent in config file {}".format(key, config_path)

        self.sections = [s for s in self.config.keys() if
                         s not in self.reserved_sections]

        for key in self.sections:
            if not self.config.get(key):
                self.config[key] = {}

        self.ftp = FTP(**(self.config['ftp']))
        self.esx = ESX(**(self.config['esx']))
        self.esx_vcenter = ESX_VCENTER(**(self.config['esx_vcenter']))
        self.settings = Settings(**(self.config['settings']))
        self.pool_name = self.settings.pool_name
        self.networks = []
        self.vms = []

        net_cfg = self.config.get('NET')
        for net_section in [s for s in self.sections if s.startswith("NET.")
                            and not self._has_child(s)]:
            cfg = self._collect_parrent_cfg(net_section)
            net_name = net_section.split(".")[-1]
            net = Network(net_name, self.pool_name, **cfg)
            self.networks.append(net)

        for net_name in self.settings.networks:
            if net_name not in [n.name for n in self.networks]:
                net = Network(net_name, self.pool_name, **net_cfg)
                self.networks.append(net)

        for vm_section in [s for s in self.sections if s.startswith("VM.")
                           and not self._has_child(s)]:
            vm_cfg = self._collect_parrent_cfg(vm_section)
            if ifaces_naming:
                vm_cfg["ifaces_naming"] = ifaces_naming
            name = vm_section.split(".")[-1]
            vm = self.vm_types[vm_cfg["type"]](name, self.pool_name,
                                               self.esx.datastore,
                                               self.networks,
                                               **vm_cfg)
            self.vms.append(vm)

    def _has_child(self, section):
        return bool([s for s in self.sections
                     if s.startswith(section) and s != section])

    def _collect_parrent_cfg(self, section):
        parent = ".".join(section.split(".")[:-1])
        if not parent:
            return deepcopy(self.config.get(section))
        else:
            cfg = self._collect_parrent_cfg(parent)
            if section.startswith("VM"):
                configuration = cfg.get("configuration")

            cfg.update(self.config.get(section))

            if section.startswith("VM") and \
                    isinstance(cfg.get("configuration"), list) and \
                    isinstance(configuration, list) and \
                    not sorted(configuration) == sorted(cfg["configuration"]):
                configuration.extend(cfg["configuration"])
                cfg["configuration"] = configuration

            return deepcopy(cfg)
