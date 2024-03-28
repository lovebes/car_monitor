#!/usr/bin/python3
import sys
import re
import time
import os
import socket
import select
import struct
import argparse
import subprocess
import signal
import traceback
import statistics
from os.path import dirname, basename, join, exists, expanduser

from utils import load_config, CONFIG, getmtime, get_iface_address

def main():
    p = argparse.ArgumentParser(description='')
    p.add_argument('files', nargs='*', help='files')
    p.add_argument('-p', '--pidfile', help='')
    p.add_argument('-l', '--logfile', help='')
    args = p.parse_args()

    load_config()

    remote_addr = CONFIG['info_server'], CONFIG['info_port']


    sock = None
    last_iface_address = -1

    idle = True

    samples = []

    next_pkt_send = 0
    next_clock_query = 0
    while True:
        try:
            saddr = get_iface_address('ppp0')
            if saddr != last_iface_address:
                last_iface_address = saddr
                if sock:
                    sock.close()
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind((saddr or '', 0))

            if not sock:
                time.sleep(5)
                continue

            if idle:
                cmtime = getmtime()
                r, w, e = select.select((sock,), (), (), max(0, min(10, next_clock_query - cmtime)))
                cmtime = getmtime()
                if cmtime >= next_clock_query:
                    next_clock_query += 600
                    if cmtime >= next_clock_query:
                        next_clock_query = cmtime + 600
                    print('query clock')
                    idle = False
                    samples = []
                    next_pkt_send = cmtime

                if sock in r:
                    pkt, addr = sock.recvfrom(256)
            else:

                cmtime = getmtime()
                r, w, e = select.select((sock,), (), (), max(0, min(10, next_pkt_send - cmtime)))
                cmtime = getmtime()

                if cmtime >= next_pkt_send:
                    next_pkt_send = cmtime + 1
                    sock.sendto(b'tt' + struct.pack('>dd', cmtime, 0.0), remote_addr)


                if sock in r:
                    pkt, addr = sock.recvfrom(256)
                    if addr == remote_addr and len(pkt) == 18 and pkt[:2] == b'tt':
                        cmtime = getmtime()
                        origmtime, cwtime = struct.unpack('>dd', pkt[2:])
                        offset = cwtime + (cmtime - origmtime) / 2 -  cmtime
                        #print(offset)
                        samples.append(offset)
                        if len(samples) >= 10:
                            samples.sort()
                            median = statistics.median(samples)
                            low = samples[0] - median
                            hi = samples[-1] - median

                            idle = True
                            calctime = cmtime + median
                            walltime = time.time()
                            diff = calctime - walltime
                            txt = 'clock drift %.3f [%.3f, %.3f]' % (diff, low, hi)
                            if abs(diff) > 0.5:
                                time.clock_settime(time.CLOCK_REALTIME, calctime)
                                txt += ' (corrected)'
                            print(txt)
        except Exception:
            traceback.print_exc()
            time.sleep(1)

if __name__ == '__main__':
    main()
