#!/usr/bin/python3
import sys
import re
import time
import os
import socket
import argparse
import subprocess
import signal
import traceback
from os.path import dirname, basename, join, exists, expanduser

from utils import load_config, CONFIG, getmtime, get_iface_address, getaddr, getmask, pack_addr, unpack_addr

STATUS_TEXT = ['NOADDR', 'PINGFAIL', 'WRONGLAN', 'READY']
STATUS_NOADDR, STATUS_PINGFAIL, STATUS_WRONGLAN, STATUS_READY = range(4)

class InterfaceManager:
    def __init__(self, iface, driver):
        self.iface = iface
        self.driver = driver
        self.last_poke = 0
        self.last_scan = 0
        self.ping_fail_count = 0
        self.last_status = None

    def get_status(self):
        addr = getaddr(self.iface)
        mask = getmask(self.iface)
        host = CONFIG['upload_host']

        self.addr = None if addr is None else unpack_addr(addr)

        if addr is None or mask is None:
            return STATUS_NOADDR

        if (pack_addr(host) & mask) != (addr & mask):
            return STATUS_WRONGLAN

        res = subprocess.call(['arping', '-c', '1', '-w', '1', '-I', self.iface, host], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res != 0:
            return STATUS_PINGFAIL

        return STATUS_READY

    def check(self):
        ctime = getmtime()
        status = self.get_status()
        if status != self.last_status:
            self.last_status = status
            print('%s status = %s' % (self.iface, STATUS_TEXT[status]))

        if status == STATUS_PINGFAIL:
            self.ping_fail_count += 1
        else:
            self.ping_fail_count = 0

        if ctime > self.last_scan + 15:
            subprocess.call(['wpa_cli', '-i', self.iface, 'scan'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.last_scan = ctime

        if self.ping_fail_count == 4 or (status == STATUS_NOADDR and (ctime > self.last_poke + 30)):
            print('poke %s' % self.iface)
            subprocess.call(['rmmod', self.driver])
            subprocess.call(['modprobe', self.driver])
            self.last_poke = ctime

        return self.addr if status == STATUS_READY else None

def flag_prop(flag):
    def get(self):
        return self.check_flag(flag)

    def set(self, val):
        self.set_flag(flag, val)

    return property(get, set, None, flag)


class Manager:
    def __init__(self):
        self.interfaces = [
            InterfaceManager('wlan0', 'brcmfmac'),
            #InterfaceManager('wlan1', '8822bu'),
        ]

        self.wifi_addr = None

    def check_flag(self, flag):
        return exists(join(self.flag_path, flag))

    def set_flag(self, flag, val):
        path = join(self.flag_path, flag)
        if val:
            with open(path, 'w') as fp:
                pass
        else:
            try:
                os.unlink(path)
            except IOError:
                pass

    def check_wifi(self):
        addr = None
        for iface in self.interfaces:
            if not exists('disable-%s' % iface.iface):
                caddr = iface.check()
                if caddr:
                    addr = caddr

        return addr


    def run(self):
        while True:
            sttime = getmtime()
            addr = None
            if exists('want-wifi'):
                addr = self.check_wifi()

            if addr != self.wifi_addr:
                self.wifi_addr = addr
                print('wifi_addr = %s' % addr)
                with open('wifi-addr', 'w') as fp:
                    fp.write('%s\n' % (addr or ''))
            ttime = getmtime() - sttime
            sleeptime = 1.0 - ttime
            if sleeptime > 0:
                time.sleep(sleeptime)
def main():
    p = argparse.ArgumentParser(description='')
    p.add_argument('files', nargs='*', help='files')
    args = p.parse_args()

    load_config()

    ctime = time.strftime('%F %T', time.localtime(time.time()))
    print('============ wifi_monitor started ============')
    mgr = Manager()
    mgr.run()

if __name__ == '__main__':
    main()
