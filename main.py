#!/usr/bin/python2.7
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

import argparse
import os
import logging
import datetime
from topology import Topology

class SmartFormatter(argparse.HelpFormatter):

    def _split_lines(self, text, width):
        # this is the RawTextHelpFormatter._split_lines
        if text.startswith('R|'):
            return text[2:].splitlines()
        return argparse.HelpFormatter._split_lines(self, text, width)

parser = argparse.ArgumentParser(
    description='''Program for deployment some topology for test needing''',
                       formatter_class=SmartFormatter)
parser.add_argument('action', help='R|Topology action:\n'
                                   'deploy - deploys topology;\n'
                                   'destroy - destroys topology;\n'
                                   'start - turn power on;\n'
                                   'stop - turn power off;\n'
                                   'restart - stop+start;\n'
                                   'reboot - restart vyatta by sending "reboot" command\n'
                                   'update - install given .deb packages to '
                                   'VMs. --packages is required;\n'
                                   'ping - check lab availability;\n'
                                   'check - validate yaml config;\n'
                                   'ssh - upload current user\'s public key '
                                   'stored in ~/.ssh/id_rsa.pub to VMs;\n'
                                   'aliases - creates aliases to VMs '
                                   'in ~/.ssh/config.\n'
                                   'Available combination of actions: '
                                   'deploy+ping+update+restart+ping')
parser.add_argument('config',
                    help='Configuration file path with topology description')
parser.add_argument('-f', '--vmfilter',
                    help='Filter by Virtual Machine name '
                         '(ex. router for "RouterA" and "RouterB")',
                    action='store',
                    default=None,
                    type=str,
                    nargs='*',
                    metavar='filter')
parser.add_argument('-i', '--iso',
                    help='ISO build image if need specific build for topology')
parser.add_argument('--no-log',
                    help='Flag for turn off save logs into file',
                    action='store_true')
parser.add_argument('--single', help='Execute all actions in one flow',
                    action='store_true')
parser.add_argument('--no-rp',
                    help='Flag for turn off creating dedicated resource pool '
                         '(only configure)', action='store_true')
parser.add_argument('-l', '--log-level',
                    help='Level of logging. Default is INFO',
                    choices=['INFO', 'DEBUG', 'WARNING', 'ERROR'],
                    default="INFO")
parser.add_argument('--ifaces_naming',
                    help='Parameter for Vyatta dataplane interfaces names. '
                         'Available values: "old" - dp0p160p1; '
                         '"new" - dp0s160. Default is "new"')
parser.add_argument('-p','--packages', help='Path to deb packages on FTP server',
                    action='store',
                    default=None,
                    type=str,
                    nargs='*',
                    metavar='package')

args, unknown = parser.parse_known_args()
logger = logging.getLogger()
logging.basicConfig(level=args.log_level,
                    format='%(asctime)-2s: %(message)-4s',
                    datefmt='%H:%M:%S')


if not args.no_log:
    log_dir = 'log'
    try:
        os.mkdir(log_dir)
    except:
        pass

    LOG_FILENAME = '%s/%s_%s.log' % (
        log_dir, datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
        args.action)
    PROFILE_FILE_NAME = '%s.%s' % (LOG_FILENAME.split('.log')[0], 'out')
    log_file = logging.FileHandler(filename=LOG_FILENAME, mode='w')
    log_file.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s %(module)s: %(message)s',
        datefmt='%m/%d/%Y %H:%M:%S')
    log_file.setFormatter(formatter)
    logger.addHandler(log_file)

if not os.path.exists(args.config):
    logger.error(
        "Configuration with '{path}' is not exist".format(path=args.config))
    exit(1)

start = datetime.datetime.now()

try:
    vmfilter = args.vmfilter if args.vmfilter and 'all' not in args.vmfilter \
        else None
    iso = args.iso if args.iso else None
    no_rp = True if args.no_rp else False
    ifaces_naming = args.ifaces_naming if args.ifaces_naming else None
    packages = args.packages if args.packages else None
    single = args.single

    try:
        tp = Topology(cfg_path=args.config,
                  vmfilter=vmfilter, no_rp=no_rp,
                  ifaces_naming=ifaces_naming, single=single)
    except Exception as e:
        if 'check' in args.action:
            logger.error("Configuration {} is not valid!".format(args.config))
            logger.error("Error message:\n{}".format(str(e)))
            exit(1)
        else:
            raise

    for action in args.action.replace("+", ",").split(","):
        if action == 'stop' or action == 'poweroff':
            tp.power_off()
        elif action == 'destroy':
            tp.destroy_vms(tp.vms) if args.vmfilter else tp.destroy()
        elif 'start' == action or 'poweron' == action:
            tp.power_on()
        elif action == 'deploy' or action == 'create':
            tp.deploy(iso=iso)
        elif action == "update" and packages:
            tp.update_with_deb(packages)
        elif action == 'reboot':
            tp.reboot_vms()
        elif action == 'restart' or action == 'reset':
            tp.reset_vms()
        elif action == "ping":
            tp.check_lab_availability()
        elif action== "configure":
            tp.configure()
        elif action == "getconfiguration":
            tp.get_configuration()
        elif action == "getctrladdr":
            tp.get_ctrl_addr()
        elif action == "check":
            logger.info("Configuration {} is valid!".format(args.config))
        elif action == "ssh":
            tp.upload_ssh_key_to_lab()
        elif action == "aliases":
            tp.create_aliases_to_lab()

except KeyboardInterrupt as e:
    logging.info('Pressed control-c; exit now')
    exit(1)

logging.info('Elapsed time (%s).' % (datetime.datetime.now() - start))

