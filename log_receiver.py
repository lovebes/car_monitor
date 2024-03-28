#!/usr/bin/python3
import sys
import re
import time
import os
import argparse
import subprocess
import traceback
import socket
import struct
import json
from os.path import dirname, basename, join, exists
from collections import OrderedDict
import logmgr
import hmac
import hotload

from binascii import b2a_hex
from hashlib import sha256

from utils import load_config, CONFIG, HMACHelper, HMACError

import log_hotload

hotload.initreload(log_hotload)


def strtime(ut):
    lt = time.localtime(ut)
    if lt.tm_isdst:
        ofs = time.altzone
    else:
        ofs = time.timezone
    ofs = ofs // 60
    if ofs < 0:
        sgn = '+'
        ofs = -ofs
    else:
        sgn = '-'
    return '%s%s%02d%02d' % (time.strftime('%F__%H-%M-%S', lt), sgn, ofs // 60, ofs % 60)

class Logger(logmgr.Log):
    sock = None

def main():
    p = argparse.ArgumentParser(description='')
    p.add_argument('files', nargs='*', help='files')
    #p.add_argument('-v', '--verbose', action='store_true', help='')
    #p.add_argument(help='')
    args = p.parse_args()

    load_config()

    verifier = HMACHelper(CONFIG['info_hmac'])

    log = Logger('logs/@@.txt')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('', 22205))

    log.sock = sock
    log_hotload.init(log)
    srcaddr = None
    while True:
        try:
            pkt, srcaddr = sock.recvfrom(256)
            ctime = strtime(time.time())
            if len(pkt) == 18 and pkt[:2] == b'tt':
                sock.sendto(pkt[:10] + struct.pack('>d', time.time()), srcaddr)

                continue

            print('%s: received packet of length %d from %r' % (ctime, len(pkt), srcaddr))
            timestamp, pkt = verifier.verify_message(pkt)

            try:
                mod, reloaded = hotload.tryreload(log_hotload, report_error=False)
            except Exception:
                print('exception loading module')
                traceback.print_exc()
                reloaded = False

            if reloaded:
                try:
                    log_hotload.init(log)
                except Exception:
                    print('exception initializing')
                    traceback.print_exc()
            try:
                log_hotload.handle_packet(log, timestamp, pkt)
            except Exception:
                traceback.print_exc()
                log.log([b2a_hex(pkt).decode('ascii')])

        except HMACError as e:
            print('Invalid packet from %r: %s' % (srcaddr, e))
        except Exception:
            traceback.print_exc()
            time.sleep(1)

if __name__ == '__main__':
    main()
