#!/usr/bin/python3
import sys
import re
import time
import os
import json
import argparse
import subprocess
import traceback
import socket
import select
import struct
import fcntl
import array

import hmac
from hashlib import sha256
from pyroute2 import iproute

from os.path import dirname, basename, join, exists, expanduser
from utils import CONFIG, load_config

from binascii import a2b_hex, b2a_hex, b2a_base64, a2b_base64

from urllib.request import Request, urlopen
from urllib.error import HTTPError

class HologramError(Exception):
    pass

def hologram_api_call(url, data):
    postdata = json.dumps(data).encode('utf8') if data else None
    try:
        req = Request(url, data=postdata, headers={'Content-Type': 'application/json'})
        u = urlopen(req)
        data = u.read()
    except HTTPError as e:
        data = e.read()

    try:
        res = json.loads(data.decode('utf8', 'ignore'))
    except ValueError:
        raise HologramError('Invalid JSON received: %r' % data)

    if res.get('success'):
        return res
    err = res.get('error')
    raise HologramError(err or 'unknown hologram error')

def send_hologram_command(cmd, progress=None):
    url = 'https://dashboard.hologram.io/api/1/devices/messages?apikey=' + CONFIG['hologram_key']
    tsnow = int(time.time() * 1000)
    msg = struct.pack('>Q', tsnow) + cmd.encode('utf8')

    pkt = hmac.HMAC(a2b_hex(CONFIG['command_hmac']), msg, sha256).digest() + msg
    print('msg = %s' % (b2a_hex(msg).decode('ascii')))
    sdata = {
        "deviceids": [CONFIG['hologram_device']],
        "base64data": b2a_base64(pkt).decode('ascii').strip().replace('\n', ''),
        #data": "wtf",
        "port": 4011,
        "protocol": 'UDP'
    }

    res = hologram_api_call(url, sdata)

    start_int = tsnow // 1000 - 5
    pollcnt = 0
    seen_id = set()
    while True:
        params = '&topics=_API_RESP_&timestart=%d' % start_int
        url = 'https://dashboard.hologram.io/api/1/csr/rdm?apikey=%s&deviceid=%d%s' % (CONFIG['hologram_key'], CONFIG['hologram_device'], params)

        res = hologram_api_call(url, None)

        if progress:
            progress('poll #%d (%d)' % (pollcnt, len(res['data'])))

        for msg in res['data']:
            id = msg['id']
            if id in seen_id:
                continue
            seen_id.add(id)

            try:
                jmsg = json.loads(msg['data'])
                #print(jmsg)
                rawdata = a2b_base64(jmsg['data'])
                #print(rawdata)
                if len(rawdata) >= 9:
                    ts, rc = struct.unpack_from('>QB', rawdata, 0)
                    if ts == tsnow:
                        return rc
            except Exception:
                traceback.print_exc()
        time.sleep(.5)
        pollcnt += 1

def main():
    p = argparse.ArgumentParser(description='')
    p.add_argument('command')
    args = p.parse_args()

    load_config()
    load_config('hologram_key')


    rc = send_hologram_command(args.command, print)
    print('result = %s' % rc)

if __name__ == '__main__':
    main()
