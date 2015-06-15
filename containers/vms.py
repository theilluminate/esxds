import logging
from containers.common import Validatable, maybe, HardwareIface


class VirtualMachine(Validatable):
    class Params:
        iso = iface_naming = hostname = maybe(str)
        user = password = default_gw = type = str
        memory = cpu = disk_space = int
        deploy = maybe(bool)

    SPORTS_DIR = 'serial_ports'
    COPY_TIMEOUT = 900

    def get_configuration_commands(self):
        raise NotImplementedError

    def __init__(self, name, pool, datastore, all_nets, **params):
        super(VirtualMachine, self).__init__(**params)
        self.name = name
        self.name_on_esx = self.get_esx_name(name, pool)

        if not self.hostname:
            self.hostname = self.name_on_esx
        self.hostname = self.hostname.replace("_", "-")

        self.disk_space *= 1024
        self.serial_dir = "/vmfs/volumes/" + datastore + "/" + self.SPORTS_DIR
        self.serial_path = self.serial_dir + "/" + self.name_on_esx
        self.deploy = self.deploy if isinstance(self.deploy, bool) else True

        assert self.type.lower() in "vyatta5400,vyatta5600,csr1000", "VM is not supported"

        self.lo_ifaces = []
        self.hw_ifaces = []
        for iface, cfg in sorted(self.ifaces.items()):
            try:
                iface_num = int(iface[-1])
            except ValueError:
                pass
            if isinstance(cfg, dict) and iface.startswith("hw"):
                temp = [net for net in all_nets if cfg["net"] == net.name]
                cfg["net"] = cfg["net"] if not temp else temp[0].name_on_esx
                iface_name = self.get_iface_name(iface_num, self.ifaces_type)
                hw_iface = HardwareIface(iface_name, **cfg)
                setattr(self, iface, hw_iface)
                setattr(self, iface_name, hw_iface)
                self.hw_ifaces.append(hw_iface)
            elif isinstance(cfg, str) and iface.startswith("lo"):
                cfg = dict(ips=cfg, name=iface)
                lo_iface = HardwareIface(**cfg)
                setattr(self, iface, lo_iface)
                self.lo_ifaces.append(lo_iface)
            else:
                logging.error("Unexpected key '" + iface +
                              "' in 'ifaces' block; ignored")
        self.ifaces = self.hw_ifaces
        self.addr = self.hw0.ipv4
        self.configuration_cmds = self.get_configuration_commands()
        if self.iso:
            self.validate_datastore_path(self.iso)

    def __str__(self):
        return self.name

    def __repr__(self):
        return '<VM> ' + self.name

    @staticmethod
    def get_iface_name(iface_num, iface_type):
        if iface_type == "ethernet":
            return "eth%d" % iface_num
        if iface_type == "dataplane":
            return VirtualMachine.map_dp_iface(iface_num)

    @staticmethod
    def map_dp_iface(iface_num):
        iface_num = int(iface_num)
        num = 160 + (32 * (iface_num % 4)) + (iface_num//4)
        return "dp0p%dp1" % num

class Vyatta(VirtualMachine):
    def __init__(self, name, pool, datastore, all_nets, **params):
        super(Vyatta, self).__init__(name, pool, datastore, all_nets, **params)
        self.login_pattern = [r"login:", r"[pP]assword:", r"\$\s", r'#\s']
        self.ssh_pattern = [r"[$#]\s",
                            r"\[sudo\] password for"]
        self.boot_pattern = [r"vyatta@vyatta.*\$", r"login:"]

    def get_configuration_commands(self):
        iface_template = ("set interface {iface_type} {name} "
                          "address {ip}/{mask}")
        vif_template = "set interface {iface_type} {name} {vif} "
        address_cmd = "address {ip}/{mask}"

        commands = ['configure',
                    "set system console device ttyS0 speed 115200",
                    "commit"]

        for iface in self.hw_ifaces:
            if iface.ipv4 and not hasattr(iface, 'vlan'):
                commands.append(iface_template.format(
                    iface_type=self.ifaces_type, name=iface.name,
                    vif="", ip=iface.ipv4, mask=iface.mask4))
            if iface.ipv6 and not hasattr(iface, 'vlan'):
                commands.append(iface_template.format(
                    iface_type=self.ifaces_type, name=iface.name,
                    vif="", ip=iface.ipv6, mask=iface.mask6))

            for vlan in iface.vlans:
                vif = "vif " + str(vlan.num)
                cmd_template = vif_template.format(iface_type=self.ifaces_type,
                                                   name=iface.name,
                                                   vif=vif)
                commands.append(cmd_template + "vlan {}".format(vlan.num))
                if vlan.ipv4:
                    cmd = cmd_template + address_cmd.format(ip=vlan.ipv4,
                                                            mask=vlan.mask4)
                    commands.append(cmd)

                if vlan.ipv6:
                    cmd = cmd_template + address_cmd.format(ip=vlan.ipv6,
                                                            mask=vlan.mask6)
                    commands.append(cmd)

        for lo in self.lo_ifaces:
            if lo.ipv4:
                commands.append(iface_template.format(
                    iface_type="loopback", name=lo.name,
                    vif="", ip=lo.ipv4, mask=lo.mask4))
            if lo.ipv6:
                commands.append(iface_template.format(
                    iface_type="loopback", name=lo.name,
                    vif="", ip=lo.ipv6, mask=lo.mask6))

        commands.extend([
            "set system host-name " + self.hostname,
            'set system login group secrets',
            'set system login user vyatta group secrets',
            'set system login user vyatta level superuser',
            'set system login user vyatta authentication plaintext-password ' + self.password,
            'set protocols static route 0.0.0.0/0 next ' + self.default_gw,
            'set service telnet', 'set service ssh',
            'set service https', 'commit', 'save', 'exit discard'])
        logging.debug("Commands for VM {}\n{}\n========".format(
            self.name, "\n".join(commands)))
        return commands


class Vyatta5600(Vyatta):
    def __init__(self, name, pool, datastore, all_nets, **params):
        super(Vyatta5600, self).__init__(name, pool, datastore, all_nets, **params)

    @staticmethod
    def get_iface_name(iface_num, iface_type):
        if iface_type == "ethernet":
            return "eth%d" % iface_num
        if iface_type == "dataplane":
            return VirtualMachine.map_dp_iface(iface_num)

    @staticmethod
    def map_dp_iface(iface_num, iface_type=None):
        iface_num = int(iface_num)
        num = 160 + (32 * (iface_num % 4)) + (iface_num//4)
        if iface_type and iface_type == "old":
            return "dp0p%dp1" % num
        return "dp0s%d" % num


class CSR1000(VirtualMachine):
    def __init__(self, name, pool, datastore, all_nets, **params):
        super(CSR1000, self).__init__(name, pool, datastore, all_nets, **params)
        # TODO: refactor me
        self.login_pattern = [r"login:", r"[pP]assword:", r"\$\s", r'#\s']
        self.ssh_pattern = [r"[$#]\s",
                            r"\[sudo\] password for"]
        self.boot_pattern = [r"vyatta@vyatta.*\$", r"login:"]

    @staticmethod
    def get_iface_name(iface_num, iface_type, naming=None):
        if iface_type == "ethernet":
            return "eth%d" % iface_num