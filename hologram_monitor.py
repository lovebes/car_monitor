#!/usr/bin/python3
import sys
import re
import time
import os
import argparse
import subprocess
import traceback
import socket
import select
import struct
import fcntl
import array

from pyroute2 import iproute

from os.path import dirname, basename, join, exists, expanduser

from utils import get_iface_address

PPP_IFACE = 'ppp0'
ROUTE_TABLE = 101

def find_modem():
    '''Search for a Hologram Nova modem'''

    basepath = '/sys/class/tty'

    # Find the lowest-numbered ttyACM# device that matches the Nova -
    # for some reason, it creates multiple devices.
    acm_devs = []
    for base in os.listdir(basepath):
        m = re.match(r'^ttyACM(\d+)$', base)
        if m:
            acm_devs.append(int(m.group(1)))

    acm_devs.sort()

    for acm_num in acm_devs:
        dev = 'ttyACM%d' % acm_num
        usbpath = join(basepath, dev, 'device', '..')
        try:
            p = join(usbpath, 'idVendor')
            with open(p) as fp:
                vendor = fp.read().strip()
            with open(join(usbpath, 'idProduct')) as fp:
                product = fp.read().strip()
            if vendor == '1546' and product == '1102':
                return dev
        except IOError:
            pass

def start_ppp(dev):
    chat_script = join(dirname(__file__), 'ppp-chat-script')
    args = ['/usr/sbin/pppd', 'connect', "/usr/sbin/chat -v -f '%s'" % chat_script, '/dev/' + dev, '9600', 'noipdefault', 'noauth', 'nodetach']
    proc = subprocess.Popen(args)
    return proc

def main():
    p = argparse.ArgumentParser(description='')
    args = p.parse_args()

    ppp_proc = None
    addr = None

    while True:
        try:
            if ppp_proc is not None:
                rc = ppp_proc.poll()
                if rc is not None:
                    print('ppp exit: %s' % rc)
                    ppp_proc = None
            else:
                modemdev = find_modem()
                if modemdev is not None:
                    print('start ppp on device %s' % modemdev)
                    ppp_proc = start_ppp(modemdev)

            new_addr = get_iface_address(PPP_IFACE)
            if new_addr != addr:
                addr = new_addr
                if addr is not None:
                    print('configuring routes for %s' % addr)
                    with iproute.IPRoute() as ipr:
                        ipr.flush_rules(table=ROUTE_TABLE)
                        ipr.flush_routes(table=ROUTE_TABLE)
                        ipr.rule('add', table=ROUTE_TABLE, src=addr)
                        ipr.route('add', table=ROUTE_TABLE, dst='0.0.0.0/0', gateway=addr)
                else:
                    print('can\'t configure routes: no address')

            time.sleep(1)

        except Exception:
            traceback.print_exc()
            time.sleep(1)

if __name__ == '__main__':
    main()
