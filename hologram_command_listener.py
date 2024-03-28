#!/usr/bin/python3
import sys
import re
import time
import os
import argparse
import traceback
import socket
import select
import struct

from os.path import dirname, basename, join, exists, expanduser

from utils import get_iface_address, load_config, CONFIG, HMACHelper, HMACError

PPP_IFACE = 'ppp0'

def main():
    p = argparse.ArgumentParser(description='')
    args = p.parse_args()

    addr = None
    sock = None
    localsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    load_config()
    verifier = HMACHelper(CONFIG['command_hmac'])

    while True:
        try:
            new_addr = get_iface_address(PPP_IFACE)
            if new_addr != addr:
                if sock:
                    sock.close()
                    sock = None

                addr = new_addr
                if addr is not None:
                    print('bind to %s' % addr)

                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((addr, 4011))
                else:
                    print('no address')

            if sock:
                r, w, e = select.select((sock,), (), (), 1.0)
            else:
                r = ()
                time.sleep(1)


            if sock in r:
                pkt, srcaddr = sock.recvfrom(1024)
                try:
                    timestamp, cmd = verifier.verify_message(pkt)
                    print('receive (%d) %r' % (timestamp, cmd))
                    localsock.sendto(cmd, ('127.0.0.1', 9900))
                    sock.sendto(struct.pack('>QB', timestamp, 1), srcaddr)
                except HMACError as e:
                    print('Invalid packet from %r: %s' % (srcaddr, e))

        except Exception:
            traceback.print_exc()
            time.sleep(1)

if __name__ == '__main__':
    main()
