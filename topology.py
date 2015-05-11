# coding=utf-8
#
# Copyright ( С ) 2013 Mirantis, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import logging
import datetime
from multiprocessing import Queue
from multiprocessing.pool import Process
from os import linesep
import os
import random
import re
import subprocess
import time
from time import sleep
from sdk2 import ExistenceException, DSApi, error_handler
from topology_reader_yaml import TopologyReader, ESX

try:
    import pexpect
except ImportError:
    pexpect = None

COMMIT_TIMEOUT = 12
INSTALL_TIMEOUT = 180
CONFIGURE_TIMEOUT = 90
LOGIN_TIMEOUT = 15


class Topology(object):
    BUILD_TIMEOUT = 1500
    OLD_BUILD_TIME = 6
    PATTERN = [r".*[#\$:] | .*: $"]
    BOOT_TIME = 300
    IFACE_COUNT = 10
    HDD_COPY_TIMEOUT = 1000

    def __init__(self, cfg_path, vmfilter=None, no_rp=None,
                 no_redeploy=None, ifaces_naming=None,
                 single=False):
        """
        Class for managing topology on ESXi server.
        @param cfg_path: path to configuration file.
        @param vmfilter: part of VM name; only VM which
        contained this string will be configured.
        @param no_networks: turn off networks creation.
        @param multi: used only in 'configure' - enable multiprocessing for
        create_vms, preconf_and_install and add_config.
        @param no_rp: turn off resource pool usage.
        @param no_redeploy: used only in 'configure' - turn off
        reliable deployment feature.
        """
        logging.basicConfig()
        self.logger = logging.getLogger(self.__module__)
        if not pexpect:
            logging.error("Warning! Deployment will not work under Windows!")
        self.single = single
        self.no_rp = no_rp
        self.no_redeploy = no_redeploy

        self.cfg = TopologyReader(cfg_path, ifaces_naming)
        self.pool_name = self.cfg.settings.pool_name
        self.esx = self.cfg.esx
        self.ftp = self.cfg.ftp
        self.all_vms = self.cfg.vms
        self.networks = self.cfg.networks

        self.isolated = [net for net in self.networks if net.isolated]
        self.shared = [net for net in self.networks if not net.isolated]
        self.lab_sw_name = self.pool_name

        # Filters Virtual machines
        self.vms = []
        if vmfilter:
            if not isinstance(vmfilter, list):
                vmfilter = [vmfilter]
            for f in vmfilter:
                self.vms.extend([vm for vm in self.all_vms if
                                 f.lower() in vm.name.lower()])
            self.vms = list(set(self.vms))
        else:
            self.vms = self.all_vms
        self.vms = [vm for vm in self.vms if vm.deploy]

        if not self.vms:
            raise Exception(
                "Could not find any host by filter '%s'" % vmfilter)
        [setattr(self, 'vm_' + vm.name, vm) for vm in self.vms]

        self.sdk = DSApi(addr=self.cfg.esx_vcenter.ip,
                         user=self.cfg.esx_vcenter.user,
                         pwd=self.cfg.esx_vcenter.password)

    def deploy(self, vms=None, iso=None):
        """
        Deploy new build images on virtual machines

        @type self: Topology
        @param vms: list of virtual machines
        @param iso: iso-image for vyatta install (relative path based on
        ftp:folder in configuration file)
        """

        vms = vms if vms else self.vms
        self.ping_hosts([self.esx, self.ftp])

        try:
            self.create_rp() if not self.no_rp else None
        except ExistenceException:
            pass
        try:
            self.create_networks()
        except ExistenceException:
            pass

        if self.ftp.target:
            self.power_off(vms, ignore_exist=True)
            if self.ftp.access == "scp":
                # TODO: refactor to 'get_build'
                self.copy_build_via_scp(iso)
            elif self.ftp.access == "nfs":
                self.create_symlink_to_iso(iso)

        self.destroy_vms(vms)
        self.create_vms(vms)
        self.power_on(vms)
        self.configure_and_install(vms=vms)
        self.power_off(vms=vms)
        self.disable_iso(vms=vms)
        self.power_on(vms=vms)
        self.add_config(vms=[vm for vm in vms if hasattr(vm, "configuration")
                             and vm.configuration])

        self.check_lab_availability(vms)

    def destroy(self):
        """
        Destroys topology
        """
        vms = self.vms
        try:
            self.destroy_vms(vms)
        except ExistenceException as e:
            logging.debug(e.message)
            pass
        try:
            self.destroy_networks()
        except ExistenceException as e:
            logging.debug(e.message)
            pass
        try:
            if not self.no_rp:
                self.destroy_rp()
        except ExistenceException as e:
            logging.debug(e.message)
            pass

    def apply_limits(self, vms=None):
        vms = vms if vms else self.vms
        pass

    def create_rp(self):
        """
        Creates a resource pool
        """
        try:
            self.sdk.create_rp(name=self.pool_name, esx_name=self.esx.name)
            logging.info('Resource pool ' + self.pool_name + ' created')
        except ExistenceException as e:
            logging.debug(e.message)
            raise

    def destroy_rp(self):
        """
        Destroys a resource pool
        """
        try:
            self.sdk.destroy_rp(name=self.pool_name, esx_name=self.esx.name)
        except ExistenceException as error:
            self.logger.info(error.message)
            pass

    def create_networks(self):
        """
        Creates ESX vSwitches and ESX port groups (networks)
        @raise: Exception
        """
        logging.info("Starting Networks creating process...")
        with PPool(self.sdk.create_vswitch) as pool:
            if self.shared:
                pool.submit(self.lab_sw_name, self.esx.name)
            for net in self.isolated:
                pool.submit(net.name_on_esx, self.esx.name)

        with PPool(self.sdk.add_portgroup) as pool:
            for net in self.shared:
                pool.submit(net.name_on_esx, self.lab_sw_name,
                            self.esx.name, net.promiscuous, net.vlan)
            for net in self.isolated:
                pool.submit(net.name_on_esx, net.name_on_esx,
                            self.esx.name, net.promiscuous, net.vlan)

    def destroy_networks(self):
        """
        Destroys ESX vSwitches and port groups
        """
        # # Destroys isolated vSwitches

        with PPool(self.sdk.destroy_vswitch) as pool:
            if self.shared:
                try:
                    pool.submit(self.lab_sw_name, self.esx.name)
                except ExistenceException:
                    pass
            for net in self.isolated:
                try:
                    pool.submit(net.name_on_esx, self.esx.name)
                except ExistenceException:
                    pass

    def create_vms(self, vms=None):
        """
        Creates virtual machines
        @param vms: list of virtual machines instances
        """
        vms = vms if vms else self.vms
        logging.info("Starting VMs creating process...")

        if self.sdk.check_pool_existence(
                self.pool_name, self.esx.name):
            rp = self.pool_name
        elif self.no_rp:
            rp = '/'
        else:
            raise ExistenceException("Couldn't specify resource pool")

        with PPool(self.sdk.create_vm) as pool:
            for vm in vms:
                kwargs = dict(vm_name=vm.name_on_esx,
                              esx_name=self.esx.name,
                              datastore=self.esx.datastore,
                              iso=vm.iso,
                              resource_pool=rp,
                              networks=[iface.network for iface
                                        in vm.hw_ifaces],
                              memorysize=vm.memory,
                              cpucount=vm.cpu,
                              disk_space=vm.disk_space,
                              serial_port=vm.serial_path,
                              hw_version=8)
                pool.submit(**kwargs)

    def destroy_vms(self, vms):
        """
        Destroys virtual machines
        @param vms: list of VirtualMachines instances
        """
        if not isinstance(vms, list):
            vms = [vms]

        logging.info("Starting VMs destroying process...")
        with PPool(self.sdk.destroy_vm) as pp:
            for vm in vms:
                pp.submit(vm.name_on_esx)
        logging.info("VMs are destroyed.")

    def power_on(self, vms=None, boottime=BOOT_TIME, ignore_exist=False):
        """
        Turns power on for topology virtual machines

        @param vms: list of VirtualMachine instances
        @param boottime: delay after turning power on
        @param ignore_exist: if VM does not exist - turn off error/traceback
        """

        vms = vms if vms else self.vms

        logging.info('Starting turning power on process...')

        if boottime:
            logging.info('Starting VMs booting process. Timeout is %s sec' %
                         str(boottime)) if boottime else None
            with PPool(self.power_on_and_wait_for_boot, single=self.single) as pp:
                for vm in vms:
                    logging.info("VM {} is booting..".format(vm.name_on_esx))
                    pp.submit(vm, boottime)

        else:
            with PPool(self.sdk.power_on_vm, single=self.single) as pp:
                for vm in vms:
                    pp.submit(vm.name_on_esx, ignore_exist)

        logging.info("VMs' power is turned on.")

    def power_off(self, vms=None, ignore_exist=False):
        """
        Turns power off for virtual machines

        @param vms: list of VirtualMachine instances
        @param ignore_exist: if VM does not exist - turn off error/traceback
        """
        vms = vms if vms else self.vms

        logging.info('Starting turning power off process...')
        with PPool(self.sdk.power_off_vm) as pp:
            for vm in vms:
                pp.submit(vm.name_on_esx, ignore_existence=ignore_exist)

        logging.info('VMs power is turned off.')

    def reset_vms(self, vms=None, boottime=BOOT_TIME, ignore_exist=False):
        self.power_off(vms, ignore_exist)
        self.power_on(vms, boottime, ignore_exist)

    def reboot_vms(self, vms=None):
        if not vms:
            vms = self.vms
        logging.info("Staring soft-reboot process...")
        with PPool(self.reboot_vm) as pp:
            for vm in vms:
                pp.submit(vm)
        logging.info("VMs are booted")

    @error_handler
    def reboot_vm(self, vm):
        conn = self.get_serial_connection_to_vyatta(vm, self.esx)
        conn.sendline("reboot")
        conn.expect(r"Proceed with reboot.*\]\s")
        conn.sendline("yes")
        sleep(5)
        conn.close()
        sleep(30)
        self.wait_for_boot(vm)
        logging.info("VM {} booted".format(vm.name))

    @error_handler
    def wait_for_boot(self, vm, timeout=BOOT_TIME):
        conn = self.open_ssh_connection(self.esx)
        conn.sendline("mkdir '/vmfs/volumes/%s/%s'" %
                      (self.esx.datastore, vm.serial_dir))
        conn.expect(ESX.ssh_pattern)
        conn_str = "nc -U '%s'" % vm.serial_path
        conn.sendline(conn_str)
        sleep(5)
        conn.expect(vm.boot_pattern, timeout=timeout - 5)
        logging.info("VM " + vm.name_on_esx + " booted")

    @error_handler
    def power_on_and_wait_for_boot(self, vm, timeout=BOOT_TIME):
        self.sdk.power_on_vm(vm.name_on_esx)
        conn = self.open_ssh_connection(self.esx)
        conn.sendline("mkdir '/vmfs/volumes/%s/%s'" %
                      (self.esx.datastore, vm.serial_dir))
        conn.expect(ESX.ssh_pattern)
        conn_str = "nc -U '%s'" % vm.serial_path
        conn.sendline(conn_str)
        sleep(5)
        conn.expect(vm.boot_pattern, timeout=timeout - 5)
        logging.info("VM " + vm.name_on_esx + " booted")


    @staticmethod
    def open_ssh_connection(host=None, ip=None, user=None, password=None):
        """
        Connects to host via SSH using pexpect library. Returns child instance.
        @param ip: host address
        @param user: host user
        @param password: host password
        """
        if not pexpect:
            raise ImportError("Pexpect not available, try to run under linux")
        assert host or (ip and user and password)
        if host:
            ip = host.ip
            user = host.user
            password = host.password

        try:
            child = pexpect.spawn(
                "ssh -oStrictHostKeyChecking=no "
                "-oUserKnownHostsFile=/dev/null %s@%s" % (user, ip))
            exp = child.expect([r".*assword: ",
                                r".*@.*[#\$].*|~ #"],
                               timeout=LOGIN_TIMEOUT)
            if exp == 0:
                child.sendline(password)
                child.expect([r".*@.*[#\$].*|~ #"])
            logging.debug("'%s' connected" % ip)
            return child
        except:
            raise Exception("Couldn\'t connect to the host %s via ssh" % ip)

    def get_serial_connection_to_vyatta(self, vm, esx=None):
        if not esx:
            esx = self.esx
        conn = self.open_ssh_connection(host=esx)
        conn.sendline("mkdir '/vmfs/volumes/%s/%s'" %
                      (self.esx.datastore, vm.serial_dir))
        conn.expect(ESX.ssh_pattern)

        # Connects via serial
        logging.debug('{} serial console is {}'.format(vm.name,
                                                       vm.serial_path))
        conn_str = "nc -U '%s'" % vm.serial_path
        conn.sendline(conn_str)
        sleep(2)

        # # VM login
        conn.sendline("")
        exp = conn.expect(vm.login_pattern, timeout=LOGIN_TIMEOUT)
        if exp == 0:
            conn.sendline(vm.user)
            exp = conn.expect(vm.login_pattern, timeout=LOGIN_TIMEOUT)
        if exp == 1:
            conn.sendline(vm.password)
            exp = conn.expect(vm.login_pattern, timeout=LOGIN_TIMEOUT)
        if exp == 3:
            conn.sendline("exit discard")
            exp = conn.expect(vm.login_pattern, timeout=LOGIN_TIMEOUT)
        if not exp == 2:
            msg = "{}: couldn't login to VM via serial connection.".format(
                vm.name_on_esx)
            logging.debug(msg)
            raise Exception(msg)
        return conn

    @error_handler
    def send_via_serial(self, vm, commands, action="send"):
        """
        Connect to vm via netcat
        pipe files for netcat are in specific directory on ESX datastore
        @param vm: VirtualMachine instance
        @param commands: list of commands
        """
        commands_log = list()
        conn = self.get_serial_connection_to_vyatta(vm)
        logging.info('%s: connected' % vm.name_on_esx)

        pattern = vm.ssh_pattern
        timeout = CONFIGURE_TIMEOUT
        err_log = ""
        # Sends commands
        for cmd in commands:
            try:
                conn.sendline(cmd)
                result = conn.expect(pattern, timeout=timeout)
                err_log +="\n======="
                err_log +="\ncmd " + cmd
                err_log +="\npattern " + str(pattern)
                err_log +="\nresult= " + str(result)
                err_log +="\nbefore " + conn.before
                err_log +="\nafter " + conn.after
                err_log +="\n======="
                if result == 1:
                    # enter password for sudo
                    err_log +="\nGOT SUDO "
                    conn.sendline(vm.password)
                    conn.expect(pattern, timeout=timeout)
                    err_log +="\nbefore " + conn.before
                    err_log +="\nafter " + conn.after
                    err_log +="\n======="
                commands_log.append('output: ' + conn.before + '\n')
                if cmd.startswith('ls'):
                    logging.info("{}:packages which will be installed:{}"
                                 "".format(vm.name_on_esx, conn.before))
            except:

                logging.error(
                    "{}:{}".format(vm.name_on_esx, linesep.join(commands_log)))
            finally:
                logging.debug(err_log)

        conn.close()
        log = linesep.join(commands_log)
        if 'Commit failed' in log \
                or 'Set failed' in log\
                or 'dpkg: error' in log:
            logging.error("{}:{}".format(vm.name_on_esx, log))
        else:
            logging.debug("{}:{}".format(vm.name_on_esx, log))
        logging.info('{}: commands were sent'.format(vm.name_on_esx))
        return vm.name_on_esx, log

    @error_handler
    def install_vyatta(self, vm):
        install_pattern = [
            r"\$",
            r"Would you like to continue\? \(Yes\/No\) \[.*\]:",
            r"Partition \(Auto\/Parted\/Skip\) \[.*\]:",
            r"Install the image on\? \[sda]\:",
            r"Continue\? \(Yes\/No\) \[No\]:",
            r"How big of a root partition should I create\? \(.+\) \[.+\]MB:",
            r"What would you like to name this image\? \[.*\]:",
            r"Enter username for administrator account \[.*\]:",
            r"Enter password for user '.*':",
            r"Retype password for user '.*':",
            r"modify the boot partition on\? \[.*\]:",
            r"Would you like to save config information from it\?",
            r"Which one should I copy\? \[.+\]:"]
        action = ["install image", "yes", "auto", "", "yes", "", "", "",
                  vm.password, vm.password, "", "no", ""]

        conn = self.get_serial_connection_to_vyatta(vm)
        logging.info('%s: connected' % vm.name_on_esx)
        try:
            conn.sendline(action[0])
            exp = conn.expect(pattern=install_pattern)
            while exp != 0:
                conn.sendline(action[exp])
                exp = conn.expect(pattern=install_pattern,
                                  timeout=INSTALL_TIMEOUT)
        finally:
            conn.close()

    def disable_iso(self, vms):
        if not isinstance(vms, list):
            vms = [vms]
        with PPool(self.sdk.detach_iso) as pp:
            for vm in vms:
                pp.submit(vm.name_on_esx)
        logging.info('The .iso image was unmounted from all VMs')

    def create_symlink_to_iso(self, iso=None):
        try:
            esx_conn = self.open_ssh_connection(host=self.esx)
        except Exception:
            raise Exception('Couldn\'t connect to ftp')

        s_datastore, s_folder = self._parse_esx_path(self.ftp.source_folder)
        s_folder = "/vmfs/volumes/%s/%s" % (s_datastore, s_folder)
        pattern = [".*[#\$] "]
        if iso:
            build = iso
        else:
            regex = re.compile('(\S+[ ]+\S+[ ]+\S+) (\S+).iso')
            esx_conn.sendline("ls -lt '%s' --color=never" % s_folder)
            esx_conn.expect(pattern)
            tmp = regex.search(esx_conn.after)
            build = tmp.group(2) + '.iso'
            logging.info("Iso " + build + " found.")

        s_iso = s_folder + build

        d_datastore, d_iso = self._parse_esx_path(self.ftp.target)
        d_iso = "/vmfs/volumes/%s/%s" % (d_datastore, d_iso)
        ln = "ln -s '%s' '%s'" % (s_iso, d_iso)

        esx_conn.sendline("rm " + d_iso)
        esx_conn.expect(pattern, timeout=self.BUILD_TIMEOUT)
        esx_conn.sendline(ln)
        esx_conn.expect(pattern, timeout=self.BUILD_TIMEOUT)
        esx_conn.sendline("echo $?")
        try:
            esx_conn.expect("\n0(\r\n|\n).*[#\$]", timeout=1)
            logging.info("Symlink to iso {} was created.".format(build))
        except pexpect.TIMEOUT:
            raise Exception('Could not create symlink %s.' % d_iso)
        esx_conn.close()

    def _get_latest_iso_name_from_ftp(self):
        try:
            ftp_conn = self.open_ssh_connection(host=self.ftp)
        except Exception:
            raise Exception('Couldn\'t connect to ftp')
        pattern = [".*[#\$] "]
        regex = re.compile('(\S+[ ]+\S+[ ]+\S+) (\S+).iso')

        try:
            logging.info("Checking builds...")
            ftp_conn.sendline(
                "ls -lt '%s' --color=never" % self.ftp.source_folder)
            ftp_conn.expect(pattern)
            tmp = regex.search(ftp_conn.after)
            iso = tmp.group(2) + '.iso'
            logging.info("ISO " + iso + " found.")
            ftp_conn.close()
            return iso
        except:
            logging.error('Couldn\'t get a build name; ls output:' +
                          str(ftp_conn.after))
            raise

    def copy_build_via_scp(self, iso=None):
        """
            Copies ftp from ftp server to the esx host
            @param iso: iso file. If not defined - latest build will be
            copied.
            """
        if iso:
            build = iso
        else:
            build = self._get_latest_iso_name_from_ftp()

        try:
            esx_conn = self.open_ssh_connection(host=self.esx)
        except Exception:
            raise Exception('Couldn\'t connect to ftp')

        datastore, iso_name = self._parse_esx_path(self.ftp.target)
        local_iso = "/vmfs/volumes/{}/{}".format(datastore, iso_name)
        remote_iso = self.ftp.source_folder + build
        # copies new build from ftp host
        scp = "scp -q -oStrictHostKeyChecking=no " \
              "-oUserKnownHostsFile=/dev/null %s@%s:'%s' '%s'" % (
                  self.ftp.user, self.ftp.ip, remote_iso, local_iso)
        logging.info('Copying build "%s"...' % build)

        pattern = [".*[#\$] ", r"[pP]assword:"]
        # Checks build availabilty
        try:
            start = datetime.datetime.now()
            # clear old image or symlink
            esx_conn.sendline("rm " + local_iso)
            esx_conn.expect(pattern, timeout=self.BUILD_TIMEOUT)

            esx_conn.sendline(scp)
            result = esx_conn.expect(pattern, timeout=self.BUILD_TIMEOUT)
            if result == 1:
                start = datetime.datetime.now()
                esx_conn.sendline(self.esx.password)
                esx_conn.expect(pattern, timeout=self.BUILD_TIMEOUT)
            elapsed_time = datetime.datetime.now() - start
            esx_conn.sendline("echo $?")
            try:
                esx_conn.expect("\n0(\r\n|\n).*[#\$]", timeout=1)
            except pexpect.TIMEOUT:
                raise Exception('Could not copy build %s.' % build)
            esx_conn.close()
            logging.info("The build '%s' copied from %s (elapsed time: %s)"
                         % (build, self.ftp.ip, elapsed_time))
        except Exception as e:
            logging.error(e.message)
            raise

    def configure_and_install(self, vms):
        """
        Configure interfaces and install vyatta on HDD
        @param: vms: VM instance or list of VM instance
        """
        if not isinstance(vms, list):
            vms = [vms]
        logging.info("Starting installation process...")

        with PPool(self.send_via_serial) as pp:
            for vm in vms:
                pp.submit(vm, vm.configuration_cmds)

        with PPool(self.install_vyatta) as pp:
            for vm in vms:
                pp.submit(vm)

        logging.info("End of installation process")


    def configure(self, vms=None):
        """
        Configure interfaces and install vyatta on HDD
        @param: vms: VM instance or list of VM instance
        """
        if not isinstance(vms, list):
            vms = self.vms
        logging.info("Starting configuring process...")

        with PPool(self.send_via_serial) as pp:
            for vm in vms:
                pp.submit(vm, vm.configuration)

        logging.info("End of configuring process")

    def add_config(self, vms):
        """
        Send additional configuration commands to VMs
        @param: vms: VM instance or list of VM instance
        """
        with PPool(self.send_via_serial) as pp:
            for vm in [vm for vm in vms if vm.configuration]:
                pp.submit(vm, vm.configuration)


    def get_ctrl_addr(self, vms=None):
        if not vms:
            vms = self.vms

        for vm in vms:
            print("{ip} {name}".format(ip=vm.addr, name=vm.name_on_esx))


    def get_configuration(self, vms=None):
        if not vms:
            vms = self.vms

        for vm in vms:
            print("\n{}({})\n{}\n{}".format(vm.name,
                                            vm.name_on_esx,
                                            "_" * 80,
                                            '\n'.join(vm.configuration + vm.configuration_cmds)))


    @staticmethod
    def ping_hosts(hosts):
        hosts = hosts if isinstance(hosts, list) else [hosts]
        for host in hosts:
            ip = host.addr if hasattr(host, "addr") else host.ip
            logging.debug('Trying to ping {}'.format(ip))
            command = ["ping", "-c 2", ip]
            executor = subprocess.Popen(command, stdout=subprocess.PIPE)
            executor.communicate()
            available = bool(not executor.returncode)
            if not available:
                msg = '%s: not available!' % ip
                raise ExistenceException(msg)
            logging.info('{} is available'.format(ip))
        return True

    def update_with_deb(self, packages, vms=None):
        if not vms:
            vms = self.vms
        logging.info("Starting installing deb packages...")
        with PPool(self.install_deb) as pp:
            for vm in vms:
                pp.submit(vm, packages)
        logging.info("Deb packages were installed!")

    @error_handler
    def install_deb(self, vm, packages):
        wget_temp = [packet for packet in packages if packet.startswith("http://")]
        scp_temp = [packet for packet in packages if not packet.startswith("http://")]

        if scp_temp:
            scp_cmd = 'scp -p -oStrictHostKeyChecking=no ' \
                      '-oUserKnownHostsFile=/dev/null ' \
                      '{user}@{ip}:"{path}" . '.format(
                path=" ".join(scp_temp), user=self.ftp.user, ip=self.ftp.ip)
        else:
            scp_cmd = ""

        if wget_temp:
            wget_cmd = 'wget {path}'.format(path=" ".join(wget_temp))
        else:
            wget_cmd = ""

        ls_cmd = "ls -al " + " ".join([pkg.split("/")[-1]
                                       for pkg in packages]) + " --color=never"

        debs = " ".join([pkg_name.split('/')[-1]
                         for pkg_name in packages
                         if pkg_name.endswith(".deb")])
        dpkg_cmd = "sudo dpkg -i " + debs
        rm_cmd = "rm " + debs

        conn = self.get_serial_connection_to_vyatta(vm, self.esx)
        conn.sendline(rm_cmd)
        conn.expect("\$\s")
        conn.sendline(wget_cmd)
        conn.expect("\$\s")
        conn.sendline(scp_cmd)
        if conn.expect(["\$\s",
                        "{}@{}'s password:".format(
                                self.ftp.user, self.ftp.ip)]) == 1:
            conn.sendline(self.ftp.password)
            conn.expect("\$\s")
        conn.sendline(ls_cmd)
        conn.expect("\$\s")
        logging.info("{}:packages which will be installed:{}".format(
            vm.name_on_esx, conn.before))
        conn.sendline(dpkg_cmd)
        if conn.expect([r"\$\s", r"\[sudo\] password for"]) == 1:
            conn.sendline(vm.password)
            conn.expect("\$\s")
        if "dpkg: error" in conn.before:
            logging.error("{}:dpkg output:{}".format(
                vm.name_on_esx, conn.before))
        else:
            logging.debug("{}:dpkg output:{}".format(
                vm.name_on_esx, conn.before))
        conn.sendline(rm_cmd)
        conn.expect("\$\s")
        conn.close()

    def check_lab_availability(self, vms=None):
        if not vms:
            vms = self.vms
        error = False
        for vm in vms:
            try:
                self.ping_hosts(vm)
            except:
                error = True
                logging.error('{} is NOT available'.format(vm.name_on_esx))
        if error:
            raise Exception("Lab not available!")
        logging.info('Lab {} is available!'.format(self.pool_name))

    @staticmethod
    def _parse_esx_path(path):
        return path.split("] ")[0][1:], path.split("] ")[1]

    def upload_ssh_key_to_lab(self, vms=None):
        if not vms:
            vms = self.vms
        public_key = open(os.path.expanduser("~/.ssh/id_rsa.pub")).read().split(" ")[1]
        with PPool(self.send_via_serial) as pool:
            for vm in vms:
                key = random.randint(1, 9999)
                commands = ["configure",
                            "set system login user {user} authentication "
                            "public-keys {key_name} type ssh-rsa".format(user=vm.user,
                                                                         key_name=key),
                            "set system login user {user} authentication "
                            "public-keys {key_name} key {public}".format(user=vm.user,
                                                                         key_name=key, public=public_key),
                            "commit", "save", "exit d"]
                pool.submit(vm, commands)

    def create_aliases_to_lab(self, vms=None):
        if not vms:
            vms = self.vms
        pattern = r"(Host {name}\n(?:\s+.+\n)*)"
        with open(os.path.expanduser("~/.ssh/config"), "r") as cfg:
            config = cfg.read()
            open(os.path.expanduser("~/.ssh/config_" + time.strftime("%d-%m-%Y_%H-%M-%S")), "w").write(config)
            for vm in vms:
                match = re.findall(pattern.format(name=vm.name_on_esx), config)
                for m in match:
                    config = config.replace(m, "")
            for vm in vms:
                config += "\nHost {name}\n" \
                          "    hostname {ip}\n" \
                          "    user {user}\n" \
                          "    Compression yes\n" \
                          "    StrictHostKeyChecking no".format(name=vm.name_on_esx,
                                                       ip=vm.addr,
                                                       user=vm.user)
            config += "\n"
        with open(os.path.expanduser("~/.ssh/config"), "w") as cfg:
            cfg.write(config)

class PPool(object):
    def __init__(self, func, single=False):
        self.func = func
        self.processes = []
        self.single = single
        self.queue = Queue()

    def submit(self, *args, **kwargs):
        kwargs["queue"] = self.queue
        p = Process(target=self.func, args=args, kwargs=kwargs)
        self.processes.append(p)
        p.start()
        if self.single:
            p.join()

    def __enter__(self):
        return self

    def __exit__(self, x, y, z):
        for p in self.processes:
            p.join()
        log = ''
        while not self.queue.empty():
            l = self.queue.get_nowait()
            log += l
            logging.error(l)
        if "Critical error!" in log:
            exit(1)
