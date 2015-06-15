import logging
import types
import netaddr


class maybe():
    def __init__(self, tp):
        self.tp = tp


class Validatable(object):
    msg = "Parameter {} has type {}, while {} expected"
    list_to_large = "Only lists of len 1 is supported as type constraints," + \
                    " not {!r}"

    class Params:
        pass

    @classmethod
    def iter_params(cls):
        for name, val in cls.Params.__dict__.items():
            if name.startswith('_'):
                continue
            if isinstance(val, (types.UnboundMethodType, types.MethodType)):
                continue
            yield name, val

    @classmethod
    def validate(cls, params):
        def check(msg_template, name, val, tp):
            if not isinstance(val, tp):
                raise ValueError(msg_template.format(name, type(val), tp))

        res = params
        for name, expected_type in cls.iter_params():
            # process lists
            if isinstance(expected_type, maybe):
                if name not in params:
                    pval = res[name] = None
                else:
                    pval = params[name]
                check(cls.msg, name, pval, (expected_type.tp, None.__class__))
                continue
            if name not in params:
                raise ValueError("Parameter {!r} absent".format(name))
            pval = params[name]
            if isinstance(expected_type, list):
                if len(expected_type) != 1:
                    raise ValueError(cls.list_to_large.format(expected_type))
                check(cls.msg, name, pval, list)
                exp_item_type = expected_type[0]
                res[name] = []
                for val in pval:
                    check(cls.msg, name, val, exp_item_type)
                    res[name].append(exp_item_type(val))
            else:
                check(cls.msg, name, pval, expected_type)
                res[name] = expected_type(pval)
        return res

    @staticmethod
    def validate_datastore_path(path):
        if not path.startswith("[") and "] " in path:
            raise AttributeError("Path '%s' doesn't match "
                                 "'[datastore] path' expression" % path)
        return True

    @staticmethod
    def get_esx_name(name, pool):
        return pool + "_" + name

    def __init__(self, **params):
        self.__dict__.update(self.__class__.validate(params))


class Aliases():
    def __init__(self, ips, vlan=None, parent=None):
        self.num = str(vlan)
        self.ipv4 = None
        self.ipv6 = None
        self.net4 = None
        self.net6 = None
        self.mask4 = None
        self.wild4 = None
        self.mask6 = None
        self.name = parent
        for ip in ips.split(","):
            if not ip:
                continue
            ip_temp = str.strip(ip)
            ip = netaddr.IPNetwork(ip_temp)
            if ip.version == 4:
                self.ipv4 = str(ip.ip)
                self.net4 = str(ip.network)
                self.mask4 = str(ip.prefixlen)
            elif ip.version == 6:
                self.ipv6 = str(ip.ip)
                self.net6 = str(ip.network)
                self.mask6 = str(ip.prefixlen)

    def __str__(self):
        return self.name + '.' + self.num

    def __repr__(self):
        return '<vlan> ' + self.name + '.' + self.num


class HardwareIface():
    def __init__(self, name, **cfg):
        self.network = cfg.get("net")
        self.name = name
        self.vlans = []
        for vlan in sorted(cfg.keys()):
            if vlan.startswith("vlan"):
                ips = Aliases(cfg[vlan]["ips"],
                              vlan=cfg[vlan]["num"],
                              parent=name)
                setattr(self, vlan, ips)
                self.vlans.append(ips)
        temp = Aliases(cfg.get("ips", ""))
        if not (temp.ipv4 or temp.ipv6) and len(self.vlans) == 1:
            vlan = self.vlans[0]
            self.own_ip = False
            self.vlan = vlan.num

            self.ipv4 = vlan.ipv4
            self.net4 = vlan.net4
            self.mask4 = vlan.mask4
            self.wild4 = vlan.wild4

            self.ipv6 = vlan.ipv6
            self.net6 = vlan.net6
            self.mask6 = vlan.mask6
        else:
            self.own_ip = True
            self.ipv4 = temp.ipv4
            self.net4 = temp.net4
            self.mask4 = temp.mask4
            self.wild4 = temp.wild4

            self.ipv6 = temp.ipv6
            self.net6 = temp.net6
            self.mask6 = temp.mask6

    def __str__(self):
        return self.name

    def __repr__(self):
        return '<interface> ' + self.name


class Network(Validatable):
    class Params:
        isolated = maybe(bool)
        promiscuous = maybe(bool)
        vlan = maybe(int)

    def __init__(self, name, pool, **params):
        super(Network, self).__init__(**params)
        self.name = name
        self.name_on_esx = self.get_esx_name(name, pool)
        self.promiscuous = params.get('promiscuous')
        self.isolated = params.get('isolated')
        vlan = params.get('vlan')
        self.vlan = vlan if vlan else 4095

    def __str__(self):
        return self.name_on_esx

    def __repr__(self):
        return '<Network> ' + self.name_on_esx

class FTP(Validatable):
    class Params:
        ip = user = password = source_folder = str
        access = target = str

    def __init__(self, **params):
        super(FTP, self).__init__(**params)
        self.ip = self.ip
        self.user = self.user
        self.password = self.password
        self.source_folder = self.source_folder
        self.access = self.access
        self.target = self.target

        if self.target:
            self.validate_datastore_path(self.target)
        if not self.source_folder.endswith("/"):
            self.source_folder += '/'
        if self.access == "nfs":
            self.validate_datastore_path(self.source_folder)


class ESX(Validatable):
    ssh_pattern = [".*[#\$:] $", ".*:$"]
    class Params:
        ip = user = name = password = datastore = str


class ESX_VCENTER(Validatable):
    class Params:
        ip = user = password = str
        datacenter = maybe(str)


class Settings(Validatable):
    class Params:
        networks = [str]
        pool_name = str

