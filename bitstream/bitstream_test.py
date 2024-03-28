#!/usr/bin/python3
import sys
import re
import time
import os
import argparse
import subprocess
import traceback
from os.path import dirname, basename, join, exists, expanduser

import bitstream

def main():
    p = argparse.ArgumentParser(description='')
    p.add_argument('files', nargs='*', help='files')
    #p.add_argument('-v', '--verbose', action='store_true', help='')
    #p.add_argument(help='')
    args = p.parse_args()

    ttybuf = bytearray(512)

    bs = bitstream.BitStream()
    tdata = b'123\x02\x02\x02A@BBCC\x03\x03456\x04C\x04\x02\x02\x02DDEEFF\x03\x03D'
    ldata = len(tdata)
    pos = 0
    while pos < ldata:
        frame, pos, ttylen = bs.parse_data(tdata, pos, ldata - pos, ttybuf)
        print(pos, ttylen, ttybuf[:ttylen])
        if frame:
            aa = bs.read_bits(15)
            bb = bs.read_bits(3)
            print(aa, bb)

    for pos in range(ldata):
        frame, npos, ttylen = bs.parse_data(tdata, pos, 1, ttybuf)
        print(pos, ttylen, ttybuf[:ttylen])
        if frame:
            aa = bs.read_bits(15)
            bb = bs.read_bits(3)
            print(aa, bb)
if __name__ == '__main__':
    main()
