#!/usr/bin/env python
import atexit

import logging
from multiprocessing import Queue
import os
from random import randint
from time import sleep
import urllib2
from pyVim import connect
import pyVmomi
from pyVmomi import vim, vmodl
import requests

# Workaround for pep-0476
import ssl
if hasattr(ssl, '_create_unverified_context'):
    ssl._create_default_https_context = ssl._create_unverified_context


class ExistenceException(Exception):
    pass


class NotFoundException(Exception):
    def __init__(self, msg):
        self.message = self.msg = msg


class OfvImportException(Exception):
    pass


def wait_for_task(task, *args, **kwargs):
    """A helper method for blocking 'wait' based on the task class.
    This dynamic helper allows you to call .wait() on any task to keep the
    python process from advancing until the task is completed on the vCenter
    or ESX host on which the task is actually running.
    Usage Examples
    ==============
    This method can be used in a number of ways. It is intended to be
    dynamically injected into the vim.Task object and these samples indicate
    that. The method may, however, be used free-standing if you prefer.
    Given an initial call similar to t
    passhis...
    code::
        rename_task = datastore.Rename('new_name')
    simple use case
    ===============
    code::
        rename_task.wait()
    The main python process will block until the task completes on vSphere.
    use with callbacks
    ==================
    Simple callback use...
    code::
        def output(task, *args):
            print task.info.state
        rename_task.wait(queued=output,
                         running=output,
                         success=output,
                         error=output)
    Only on observed task status transition will the callback fire. That is
    if the task is observed leaving queued and entering running, then the
    callback for 'running' is fired.
    :type task: vim.Task
    :param task: any subclass of the vim.Task object
    :rtype None: returns or raises exception
    :raises vim.RuntimeFault:
    """

    def no_op(task, *args):
        pass

    queued_callback = kwargs.get('queued', no_op)
    running_callback = kwargs.get('running', no_op)
    success_callback = kwargs.get('success', no_op)
    error_callback = kwargs.get('error', no_op)

    si = connect.GetSi()
    pc = si.content.propertyCollector
    obj_spec = [vmodl.query.PropertyCollector.ObjectSpec(obj=task)]
    prop_spec = vmodl.query.PropertyCollector.PropertySpec(type=vim.Task,
                                                           pathSet=[],
                                                           all=True)

    filter_spec = vmodl.query.PropertyCollector.FilterSpec()
    filter_spec.objectSet = obj_spec
    filter_spec.propSet = [prop_spec]
    filter = pc.CreateFilter(filter_spec, True)

    try:
        version, state = None, None

        # Loop looking for updates till the state moves to a completed state.
        waiting = True
        while waiting:
            update = pc.WaitForUpdates(version)
            version = update.version
            for filterSet in update.filterSet:
                for objSet in filterSet.objectSet:
                    task = objSet.obj
                    for change in objSet.changeSet:
                        if change.name == 'info':
                            state = change.val.state
                        elif change.name == 'info.state':
                            state = change.val
                        else:
                            continue

                        if state == vim.TaskInfo.State.success:
                            success_callback(task, *args)
                            waiting = False

                        elif state == vim.TaskInfo.State.queued:
                            queued_callback(task, *args)

                        elif state == vim.TaskInfo.State.running:
                            running_callback(task, *args)

                        elif state == vim.TaskInfo.State.error:
                            error_callback(task, *args)
                            raise task.info.error

    finally:
        if filter:
            filter.Destroy()

vim.Task.wait = wait_for_task


def error_handler(func):
    def catcher(*args, **kwargs):
        queue = kwargs.get("queue")
        if queue:
            del kwargs["queue"]
        try:
            func(*args, **kwargs)
        except Exception as e:
            if queue:
                #queue.put(str(e))
                msg = ""
                if hasattr(e, "message"):
                    #logging.error("1")
                    logging.debug(e.message)
                    msg += str(e.message)
                if hasattr(e, "msg") and not e.msg == msg:
                    #logging.error("2")
                    logging.debug(e.msg)
                    msg += str(e.msg)
                if hasattr(e, "value"):
                    #logging.error("3")
                    logging.debug(e.value)
                    msg += str(e.value)
                #raise
                queue.put(msg)
            else:
                raise
    return catcher



class DSApi:
    def __init__(self, addr, user, pwd):
        self.addr = addr
        self.user = user
        self.pwd = pwd
        self.esx = None
        try:
            import requests.packages.urllib3
            requests.packages.urllib3.disable_warnings()
        except:
            pass
        try:
            import urllib3
            urllib3.disable_warnings()
        except:
            pass

        logging.getLogger("requests").propagate = False
        # self.esx = connect.SmartConnect(host=self.addr,
        #                                 user=self.user,
        #                                 pwd=self.pwd)
        # self.content = self.esx.RetrieveContent()
        # atexit.register(connect.Disconnect, self.esx)
        logging.getLogger("requests").propagate = True

    def reconnect(self):
        logging.getLogger("requests").propagate = False
        #requests.packages.urllib3.disable_warnings()
        try:
            self.content = self.esx.RetrieveContent()
        except:
            self.esx = connect.SmartConnect(host=self.addr,
                                            user=self.user,
                                            pwd=self.pwd)
            self.content = self.esx.RetrieveContent()
        finally:
            logging.getLogger("requests").propagate = True

    def collect_properties(self, obj_type, container=None, path_set=None,
                           include_mors=False):
        """
        Collect properties for managed objects from a view ref
        Check the vSphere API documentation for example on retrieving
        object properties:
            - http://goo.gl/erbFDz
        Args:
            si          (ServiceInstance): ServiceInstance connection
            view_ref (pyVmomi.vim.view.*): Starting point of inventory navigation
            obj_type      (pyVmomi.vim.*): Type of managed object
            path_set               (list): List of properties to retrieve
            include_mors           (bool): If True include the managed objects
                                           refs in the result
        Returns:
            A list of properties for the managed objects
        """
        # logging.error("Enter Collect_props")
        if not container:
            container = self.esx.content.rootFolder
        # logging.error("456")
        # logging.error("123")
        view_ref = self.esx.content.viewManager.CreateContainerView(
            container=container,
            type=[obj_type],
            recursive=True
        )

        collector = self.esx.content.propertyCollector

        # Create object specification to define the starting point of
        # inventory navigation
        obj_spec = pyVmomi.vmodl.query.PropertyCollector.ObjectSpec()
        obj_spec.obj = view_ref
        obj_spec.skip = True

        # Create a traversal specification to identify the path for collection
        traversal_spec = pyVmomi.vmodl.query.PropertyCollector.TraversalSpec()
        traversal_spec.name = 'traverseEntities'
        traversal_spec.path = 'view'
        traversal_spec.skip = False
        traversal_spec.type = view_ref.__class__
        obj_spec.selectSet = [traversal_spec]

        # Identify the properties to the retrieved
        property_spec = pyVmomi.vmodl.query.PropertyCollector.PropertySpec()
        property_spec.type = obj_type

        if not path_set:
            property_spec.all = True

        property_spec.pathSet = path_set

        # Add the object and property specification to the
        # property filter specification
        filter_spec = pyVmomi.vmodl.query.PropertyCollector.FilterSpec()
        filter_spec.objectSet = [obj_spec]
        filter_spec.propSet = [property_spec]

        # Retrieve properties
        props = collector.RetrieveContents([filter_spec])

        data = []
        for obj in props:
            properties = {}
            for prop in obj.propSet:
                properties[prop.name] = prop.val

            if include_mors:
                properties['obj'] = obj.obj

            data.append(properties)
        return data

    @staticmethod
    def _get_from_list(lst, attr="name", val=None):
        for l in lst:
            if getattr(l, attr) == val:
                return l
        lst = str([(getattr(l, attr), str(l)) for l in lst])
        raise NotFoundException("Couldn't find field '%s' with value '%s' "
                                "in objects:\n%s" % (attr, val, lst))

    def _get_datacenter_mor(self, dc_name=None):
        self.reconnect()
        if not dc_name:
            return self.content.rootFolder.childEntity[0]

    def _get_datastore_mor(self, ds_name):
        return self._get_from_list(self._get_datacenter_mor().datastore,
                                   "name", ds_name)

    def _get_compute_mor(self, esx_name):
        # enhansment available: multiple datacenter support
        datacenter = self._get_datacenter_mor()
        compute = [compute for compute in datacenter.hostFolder.childEntity
                   if compute.host[0].name == esx_name]
        if not compute:
            raise NotFoundException("Couldn't get the host mor of "
                                    "esx '%s'" % esx_name)
        return compute[0]

    def _get_host_mor(self, esx_name):
        return self._get_compute_mor(esx_name).host[0]

    def _get_network_system_mor(self, esx_name):
        return self._get_host_mor(esx_name).configManager.networkSystem

    def _get_pool_mor(self, pool_name, esx_name):
        pool = self._get_compute_mor(esx_name).resourcePool
        if (pool_name == '/' or pool_name == 'Resources'):
            return pool
        for subpool in pool_name.split("/"):
            pool = self._get_from_list(pool.resourcePool, val=subpool)
        return pool

    def _get_port_group_mor(self, name, esx_name):
        networks = self._get_compute_mor(esx_name).network
        return self._get_from_list(networks, "name", name)

    def _get_ovf_manager_mor(self):
        self.reconnect()
        return self.content.ovfManager

    def _wait_until(self, var, value, timeout=60):
        for i in xrange(timeout):
            if var == value:
                return True
            sleep(1)
        raise Exception("Timeout")

    def get_all_vms(self):
        props = self.collect_properties(obj_type=vim.VirtualMachine,
                                   path_set=["name"], include_mors=True)
        return [p["obj"] for p in props]

    def get_vm_mor(self, vm_name):
        self.reconnect()
        # logging.error("Enter 'get vm mor' for vm " + vm_name)
        props = self.collect_properties(obj_type=vim.VirtualMachine,
                                   path_set=["name"], include_mors=True)
        # logging.error("Collected 'get vm mor' for vm " + vm_name)
        # logging.error(str(props))
        for p in props:
            if p["name"] == vm_name:
                # logging.error("vm " + vm_name + " found!")
                return p["obj"]
        return None

    def get_vm_files(self, vm_name):
        return self.get_vm_mor(vm_name).config.files.vmPathName

    def check_vm_existence(self, vm_name):
        return bool(self.get_vm_mor(vm_name))

    @error_handler
    def create_vm(self, vm_name, esx_name, datastore, iso=None,
                  resource_pool='/', networks=None, guestid="debian4Guest",
                  serial_port=None, hw_version=None, memorysize=512,
                  cpucount=1, disk_space=1048576):
        self.reconnect()
        if not networks:
            networks = []
        datacenter = self._get_datacenter_mor()
        vm_folder = datacenter.vmFolder
        resource_pool = self._get_pool_mor(resource_pool, esx_name)
        compute = self._get_compute_mor(esx_name)
        host = self._get_host_mor(esx_name)
        default_devs = compute.environmentBrowser. \
            QueryConfigOption(host=host).defaultDevice
        conf_target = compute.environmentBrowser.QueryConfigTarget(host=host)
        devices = []

        if not [ds for ds in conf_target.datastore
                if ds.datastore.name == datastore]:
            raise NotFoundException("Datastore '%ds' not found" % datastore)
        vm_path = "[%s] %s" % (datastore, vm_name)

        connectable = vim.vm.device.VirtualDevice.ConnectInfo(
            startConnected=True)
        if iso:
            assert iso.startswith('[') and '] ' in iso and iso.endswith(".iso")
            ide_ctrl = [dev for dev in default_devs if
                        isinstance(dev, vim.vm.device.VirtualIDEController)][0]
            iso_ds = self._get_datastore_mor(iso.split("] ")[0][1:])
            backing = vim.vm.device.VirtualCdrom.IsoBackingInfo(
                fileName=iso, datastore=iso_ds)
            cdrom = vim.vm.device.VirtualCdrom(backing=backing,
                                               key=3050 + randint(0, 99),
                                               connectable=connectable,
                                               controllerKey=ide_ctrl.key,
                                               unitNumber=0)
            cdrom_spec = vim.vm.device.VirtualDeviceSpec(operation="add",
                                                         device=cdrom)
            devices.append(cdrom_spec)

        if disk_space != 0:
            scsi_key = 1
            scsi_ctrl = vim.vm.device.VirtualLsiLogicController(
                busNumber=0, key=scsi_key, sharedBus="noSharing")
            scsi_spec = vim.vm.device.VirtualDeviceSpec(operation="add",
                                                        device=scsi_ctrl)
            devices.append(scsi_spec)

            backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
                diskMode="persistent", thinProvisioned=True)
            hdd = vim.vm.device.VirtualDisk(capacityInKB=disk_space,
                                            key=randint(50, 100),
                                            backing=backing,
                                            connectable=connectable,
                                            controllerKey=scsi_key,
                                            unitNumber=0)
            hdd_spec = vim.vm.device.VirtualDeviceSpec(operation="add",
                                                       fileOperation="create",
                                                       device=hdd)
            devices.append(hdd_spec)

        for net in networks:
            if not [n for n in conf_target.network if n.name == net]:
                raise NotFoundException(msg="Critical error! "
                                        "Network " + net + " is not exists")
            net_mor = self._get_from_list(host.network, "name", net)
            backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
                deviceName=net,
                network=net_mor)
            network = vim.vm.device.VirtualVmxnet3(
                addressType="generated",
                backing=backing,
                connectable=connectable,
                key=randint(4005, 4999))
            network_spec = vim.vm.device.VirtualDeviceSpec(operation="add",
                                                           device=network)
            devices.append(network_spec)

        if serial_port:
            sio_ctrl = [dev for dev in default_devs if
                        isinstance(dev, vim.vm.device.VirtualSIOController)][0]
            backing = vim.vm.device.VirtualSerialPort.PipeBackingInfo(
                endpoint="server",
                pipeName=serial_port)
            com_port = vim.vm.device.VirtualSerialPort(
                backing=backing,
                key=9000,
                controllerKey=sio_ctrl.key,
                unitNumber=0,
                yieldOnPoll=True)
            com_spec = vim.vm.device.VirtualDeviceSpec(operation="add",
                                                       device=com_port)
            devices.append(com_spec)

        if isinstance(hw_version, int):
            vm_version = "vmx-" + (str(hw_version) if hw_version > 9
                                   else "0" + str(hw_version))
        else:
            vm_version = "vmx-08"

        vmx_file = vim.vm.FileInfo(logDirectory=None,
                                   snapshotDirectory=None,
                                   suspendDirectory=None,
                                   vmPathName=vm_path)

        config = vim.vm.ConfigSpec(name=vm_name,
                                   version=vm_version,
                                   guestId=guestid,
                                   files=vmx_file,
                                   numCPUs=cpucount,
                                   numCoresPerSocket=cpucount,
                                   memoryMB=memorysize,
                                   deviceChange=devices,
                                   cpuAllocation=vim.ResourceAllocationInfo(
                                       limit=2000),
                                   memoryAllocation=vim.ResourceAllocationInfo(
                                       limit=memorysize),
                                   swapPlacement="hostLocal")

        try:
            vm_folder.CreateVM_Task(config=config, pool=resource_pool).wait()
        except Exception as e:
            logging.debug(str(vm_name))
            logging.debug(e)
            if hasattr(e, "msg"):
                logging.debug(str(e.msg))
            if hasattr(e, "message"):
                logging.debug(str(e.message))
            raise

    @error_handler
    def detach_iso(self, vm_name):
        self.power_off_vm(vm_name)
        vm_mor = self.get_vm_mor(vm_name)
        cdrom = [dev for dev in vm_mor.config.hardware.device
                 if isinstance(dev, vim.vm.device.VirtualCdrom)]
        if not cdrom:
            return
        cdrom = cdrom[0]
        backing = vim.vm.device.VirtualCdrom.RemoteAtapiBackingInfo(
            deviceName="")
        cdrom.backing = backing
        cdrom_spec = vim.vm.device.VirtualDeviceSpec(operation="edit",
                                                     device=cdrom)
        vm_mor.ReconfigVM_Task(
            vim.vm.ConfigSpec(deviceChange=[cdrom_spec])).wait()

    @error_handler
    def fix_resource_allocation(self, vm_name, cpu_limit=2000,
                                memory_limit=None):
        vm_mor = self.get_vm_mor(vm_name)
        memory_limit = memory_limit if memory_limit else \
            vm_mor.config.hardware.memoryMB
        shares = vim.SharesInfo(level="normal")
        cpu_allocation = vim.ResourceAllocationInfo(
            limit=cpu_limit,
            shares=shares)
        memory_alloc = vim.ResourceAllocationInfo(
            limit=memory_limit,
            shares=shares)
        vm_mor.ReconfigVM_Task(
            vim.vm.ConfigSpec(cpuAllocation=cpu_allocation,
                              memoryAllocation=memory_alloc)).wait()

    @error_handler
    def destroy_vm(self, vm_name):
        try:
            self.power_off_vm(vm_name)
            self.get_vm_mor(vm_name).Destroy_Task().wait()
        except NotFoundException:
            pass

    @error_handler
    def power_on_vm(self, vm_name, ignore_existence=False):
        try:
            vm = self.get_vm_mor(vm_name)
            vm.PowerOn() if vm.runtime.powerState != "poweredOn" else None
            timeout = 10
            while timeout and vm.runtime.powerState != "poweredOn":
                sleep(1)
                self.reconnect()
                timeout -= 1
        except:
            if ignore_existence:
                return
            raise

    @error_handler
    def power_off_vm(self, vm_name, ignore_existence=False):
        try:
            vm = self.get_vm_mor(vm_name)

            if vm.runtime.powerState == "poweredOn":
                vm.PowerOff()
            timeout = 10
            # logging.error(vm_name + "|" + vm.runtime.powerState)
            while timeout and vm.runtime.powerState != "poweredOff":
                sleep(1)
                self.reconnect()
                # logging.error(vm_name + "|" + vm.runtime.powerState)
                timeout -= 1
        except:
            if ignore_existence:
                return
            raise

    @error_handler
    def reset_vm(self, vm_name):
        self.get_vm_mor(vm_name).ResetVM().wait()

    def check_pool_existence(self, name, esx_name):
        try:
            self._get_pool_mor(name, esx_name)
            return True
        except:
            return False

    @error_handler
    def create_rp(self, name, esx_name, parent="/"):
        if self.check_pool_existence(name, esx_name):
            raise ExistenceException("Resource pool %s already exists "
                                     "on esx %s" % (name, esx_name))
        root_pool = self._get_pool_mor(parent, esx_name)
        cpu_alloc = vim.ResourceAllocationInfo(
            shares=vim.SharesInfo(level='normal'),
            limit=-1,
            expandableReservation=True,
            reservation=0)
        memory_alloc = vim.ResourceAllocationInfo(
            shares=vim.SharesInfo(level='normal'),
            limit=-1,
            expandableReservation=True,
            reservation=0)
        pool_spec = vim.ResourceConfigSpec(cpuAllocation=cpu_alloc,
                                           memoryAllocation=memory_alloc)
        try:
            root_pool.CreateResourcePool(name=name, spec=pool_spec)
        except vim.fault.DuplicateName as e:
            raise ExistenceException(e.msg)

    @error_handler
    def destroy_rp(self, name, esx_name):
        if self.check_pool_existence(name, esx_name):
            self._get_pool_mor(name, esx_name).Destroy().wait()


    def check_vswitch_existence(self, name, esx_name):
        return bool([
            sw for sw in self._get_network_system_mor(esx_name
            ).networkInfo.vswitch if sw.name == name])

    @error_handler
    def create_vswitch(self, name, esx_name, ports=128):
        net_system = self._get_network_system_mor(esx_name)
        if self.check_vswitch_existence(name, esx_name):
            raise ExistenceException("Switch '%s' already exists "
                                     "on esx '%s'" % (name, esx_name))

        spec = vim.host.VirtualSwitch.Specification(numPorts=ports)
        net_system.AddVirtualSwitch(name, spec)
        counter = 20
        while counter and not self.check_vswitch_existence(name, esx_name):
            counter -= 1
            sleep(1)

    @error_handler
    def destroy_vswitch(self, name, esx_name):
        net_system = self._get_network_system_mor(esx_name)
        if self.check_vswitch_existence(name, esx_name):
            net_system.RemoveVirtualSwitch(name)

    def check_portgroup_existence(self, name, esx_name):
        net_system = self._get_network_system_mor(esx_name)
        return bool([port for port in net_system.networkInfo.portgroup
                    if port.spec.name == name])

    @error_handler
    def add_portgroup(self, name, sw_name, esx_name, promisc=False, vlan=4095):
        net_system = self._get_network_system_mor(esx_name)

        if self.check_portgroup_existence(name, esx_name):
            raise ExistenceException("PortGroup '%s' already exists "
                                     "on esx '%s'" % (name, esx_name))

        policy = vim.host.NetworkPolicy(
            security=vim.host.NetworkPolicy.SecurityPolicy(
                allowPromiscuous=promisc))
        s = vim.host.PortGroup.Specification(name=name,
                                             vlanId=vlan,
                                             vswitchName=sw_name,
                                             policy=policy)
        net_system.AddPortGroup(s)
        counter = 20
        while counter and not self.check_portgroup_existence(name, esx_name):
            counter -= 1
            sleep(1)

    def get_snapshot_by_name(self, vm_mor, snap_name):
        def get(snap_list, name):
            print ">enter " + name
            for snap in snap_list:
                if snap.name == name:
                    print "<found! leave " + snap.name
                    return snap
                if not snap.childSnapshotList:
                    print "<leaf - leave " + snap.name
                    return None
                get(snap.childSnapshotList, name)

        return get(vm_mor.snapshot.rootSnapshotList, snap_name)


    @error_handler
    def create_snapshot(self, vm_name, snap_name, descriprion=None):
        vm = self.get_vm_mor(vm_name)
        snap = self.get_snapshot_by_name(vm, snap_name)
        if snap:
            print snap.name
            print snap.description


    def deploy_ovf(self, vm_name, path, esx_name, resource_pool, datastore,
                   network_mapping=None):
        if not network_mapping:
            network_mapping = {}
        host_mor = self._get_host_mor(esx_name)
        pool_mor = self._get_pool_mor(resource_pool, esx_name)
        vm_folder = self._get_datacenter_mor().vmFolder
        ovf_net_mapping = [
            vim.OvfManager.NetworkMapping(name=ovf_net,
                                          network=self._get_from_list(
                                              host_mor.network, "name",
                                              esx_net))
            for ovf_net, esx_net in network_mapping.items()
        ]

        import_spec_params = vim.OvfManager.CreateImportSpecParams(
            entityName=vm_name,
            hostSystem=host_mor,
            networkMapping=ovf_net_mapping,
            diskProvisioning="thin")

        ovf_data = open(path, "r").read()

        import_spec = self._get_ovf_manager_mor().CreateImportSpec(
            ovfDescriptor=ovf_data,
            resourcePool=pool_mor,
            datastore=self._get_datastore_mor(datastore),
            cisp=import_spec_params)
        if import_spec.error:
            raise OfvImportException()
        for warning in import_spec.warning:
            logging.warning("OVF Warning: " + warning.msg)
        nfc_lease = pool_mor.ImportVApp(spec=import_spec.importSpec,
                                        folder=vm_folder,
                                        host=host_mor)
        self._wait_until(nfc_lease.state, "initializing")
        if nfc_lease.state == "error":
            raise nfc_lease.error

        nfc_lease.HttpNfcLeaseProgress(percent=5)
        for file_item in import_spec.fileItem:
            while nfc_lease.state != "ready":
                sleep(1)

            device_url = [device for device in nfc_lease.info.deviceUrl
                          if device.importKey == file_item.deviceId]
            if not device_url or len(device_url)>1:
                raise Exception()
            else:
                device_url = device_url[0].url
            method = "PUT" if file_item.create else "POST"
            method = "POST"
            file_location = "/".join(path.split("/")[:-1]) + "/" + file_item.path
            template = "curl -L file://{local} | curl -Ss -X {method} --insecure -T - -H " \
                       "'Content-Type: application/x-vnd.vmware-streamVmdk' " \
                       "'{remote}'".format(local=file_location,
                                           method=method, remote=device_url)
            template = "cat {local} | curl -Ss -X {method} --insecure -T - -H " \
                       "'Content-Type: application/x-vnd.vmware-streamVmdk' " \
                       "'{remote}'".format(local=file_location,
                                           method=method, remote=device_url)
            print template
            os.system(template)

        nfc_lease.HttpNfcLeaseComplete()
        return

if __name__ == "__main__":
    ds = DSApi("172.18.93.40", "root", "vmware")
    # ds.create_vm("netw_test", "172.18.93.30","datastore1",
    #              networks=["VLAN1006","qweqwe"])
    try:
        ds.destroy_vm("ovf_test")
    except:
        pass
    ds.deploy_ovf("ovf_test",
                  "/home/vkhlyunev/temp/ovf/csr1000v-universalk9.03.12.00.S.154-2.S-std.ovf",
                  "172.18.93.30","/", "datastore1",
                  {"GigabitEthernet1":"VLAN1006"})
