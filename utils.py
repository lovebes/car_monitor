import sys
import re
import time
import os
import socket
import fcntl
import json
import struct
import hmac
from hashlib import sha256

from os.path import dirname, join, exists
from binascii import a2b_hex, b2a_hex

GPIO_BASE_PATH = '/sys/class/gpio'
PWM_BASE_PATH = '/sys/class/pwm/pwmchip0'

CONFIG = {}

def getmtime():
    return time.clock_gettime(time.CLOCK_MONOTONIC)

def load_config(name='config'):
    config_path = join(dirname(__file__), name + '.json')
    try:
        with open(config_path, 'r') as fp:
            CONFIG.update(json.load(fp))
    except FileNotFoundError:
        print('ERROR: {0}.json not found. Edit {0}_sample.json and save as {0}.json.'.format(name), file=sys.stderr)
        sys.exit(0)

def setup_gpio(pin):
    try:
        dir = join(GPIO_BASE_PATH, 'gpio%d/direction' % pin)
        if not exists(dir):
            try:
                with open(join(GPIO_BASE_PATH, 'export'), 'w') as fp:
                    fp.write(str(pin))
            except IOError as e:
                print('error exporting %d: %s' % (pin, e))

        with open(dir) as fp:
            cd = fp.read().strip()

        if cd != 'out':
            with open(dir, 'w') as fp:
                fp.write('out')

    except IOError as e:
        print('WARNING: could not set up GPIO pin %d: %s' % (pin, e))

def set_gpio(pin, val):
    try:
        with open(join(GPIO_BASE_PATH, 'gpio%d/value' % pin), 'w') as fp:
            fp.write('1' if val else '0')
    except IOError as e:
        print('WARNING: could not set GPIO pin %d: %s' % (pin, e))

def setup_pwm(pin):
    try:
        enable = join(PWM_BASE_PATH, 'pwm%d/enable' % pin)
        if not exists(enable):
            try:
                with open(join(PWM_BASE_PATH, 'export'), 'w') as fp:
                    fp.write(str(pin))
            except IOError as e:
                print('error exporting %d: %s' % (pin, e))

        set_pwm_freq(pin, 440)
    except IOError as e:
        print('WARNING: could not set up PWM pin %d: %s' % (pin, e))

def set_pwm_enable(pin, val):
    try:
        with open(join(PWM_BASE_PATH, 'pwm%d/enable' % pin), 'w') as fp:
            fp.write('1' if val else '0')
    except IOError as e:
        print('WARNING: could not set PWM pin %d: %s' % (pin, e))

def set_pwm_freq(pin, freq):
    try:

        # period is in nanoseconds
        period = int(1000000000 / freq)

        with open(join(PWM_BASE_PATH, 'pwm%d/duty_cycle' % pin), 'w') as duty_fp:
            with open(join(PWM_BASE_PATH, 'pwm%d/period' % pin), 'w') as period_fp:
                duty_fp.write('0')
                period_fp.write(str(period))
                duty_fp.seek(0)
                duty_fp.write(str(period // 2))
    except IOError as e:
        print('WARNING: could not set PWM pin %d freq: %s' % (pin, e))

def hexdump(s):
    rdata = []
    for b in range(0, len(s), 16):
        lin = [c for c in s[b : b + 16]]
        hxdat = ' '.join('%02X' % c for c in lin)
        pdat = ''.join((chr(c) if 32 <= c <= 126 else '.') for c in lin)
        rdata.append('  %04x: %-48s %s' % (b, hxdat, pdat))
    return rdata

def crc16(buf, spos):
    crc = 0xFFFF

    for i in range(spos, len(buf)):
        x = ((crc >> 8) ^ buf[i]) & 0xFF
        x ^= x >> 4
        crc = ((crc << 8) ^ (x << 12) ^ (x <<5) ^ x) & 0xFFFF

    return crc

def ndbg(t):
    pass

def get_iface_address(iface):
    addr = getaddr(iface)
    if addr is None:
        return None
    addr = unpack_addr(addr)
    return addr

def _get_iface_info(iface, ctl):
    buf = bytearray(40)
    iface = bytes(iface, 'utf-8')
    buf[:len(iface)] = iface
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    saddr = None
    try:
        fd = s.fileno();
        res = fcntl.ioctl(fd, ctl, buf)
        return struct.unpack_from(">I", buf, 20)[0]
    except OSError:
        return None
    finally:
        s.close()
    return None

def pack_addr(str):
    if str is None:
        return None
    try:
        return struct.unpack(">I", socket.inet_aton(str))[0]
    except socket.error:
        return 0

def unpack_addr(addr):
    return '.'.join(str((addr >> i) & 255) for i in range(24, -8, -8))

def addr_on_lan(iface, addr):
    myaddr = getaddr(iface)
    mymask = getmask(iface)
    if myaddr is not None and mymask is not None:
        return (pack_addr(addr) & mymask) == (myaddr & mymask)
    return False

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891b
def getaddr(iface):
    return _get_iface_info(iface, SIOCGIFADDR)

def getmask(iface):
    return _get_iface_info(iface, SIOCGIFNETMASK)

class BitStream:
    def __init__(self):
        self.buffer = b''
        self.buffer_bitpos = 0
        self.buffer_wordpos = 0
        self.raw_list = None
        self.dbg = None

    def reset(self):
        self.buffer_bitpos = 0
        self.buffer_wordpos = 0

    def read_bits(self, nbits):
        mask = (1 << nbits) - 1
        if self.raw_list is not None:
            rv = self.raw_list[self.buffer_wordpos]
            self.buffer_wordpos += 1
            return rv & mask

        wpos = self.buffer_wordpos
        bpos = self.buffer_bitpos
        buf = self.buffer
        buflen = len(buf)

        #if self.dbg:
        #    self.dbg('read_bits(%d): wpos = %d bpos = %d' % (nbits, wpos, bpos))
        if wpos >= buflen:
            return 0

        rv = (buf[wpos] | (buf[wpos + 1] << 8)) >> bpos
        bits_copied = 15 - bpos
        bpos += nbits
        if bpos < 15:
            #if self.dbg:
            #    self.dbg(' ... 1: %x' % (rv & mask))
            self.buffer_bitpos = bpos
            return rv & mask

        nbits -= bits_copied
        #if self.dbg:
        #    self.dbg(' bits rem = %d' % nbits)

        while nbits > 0:
            wpos += 2
            if wpos >= buflen:
                val = 0
            else:
                val = buf[wpos] | (buf[wpos + 1] << 8)
            rv |= val << bits_copied
            bits_copied += 15
            nbits -= 15
        if nbits == 0:
            self.buffer_bitpos = 0
            self.buffer_wordpos = wpos + 2
        else:
            self.buffer_bitpos = nbits + 15
            self.buffer_wordpos = wpos
        #if self.dbg:
        #    self.dbg(' ... 2: %x' % (rv & mask))
        return rv & mask

    def read_bits_signed(self, nbits):
        rv = self.read_bits(nbits)
        if (rv & (1 << (nbits - 1))) != 0:
            rv -= 1 << nbits
        return rv

class HMACError(Exception):
    pass

class HMACHelper:
    '''Verifies an HMAC signature using a pre-shared key. Packet format is:
      pkt[0:32]  = HMAC-SHA256 of pkt[32:]
      pkt[32:40] = milliseconds since UNIX epoch, big endian
      pkt[40:]   = payload
    '''

    def __init__(self, keytxt, window=180000):
        self.hmac = hmac.HMAC(a2b_hex(keytxt), None, sha256)
        self.window = window
        self.timestamps_seen = []

    def build_message(self, payload, ts=None):
        if ts is None:
            ts = int(time.time() * 1000)
        msg = struct.pack('>Q', ts) + payload

        nhmac = self.hmac.copy()
        nhmac.update(msg)
        return nhmac.digest() + msg

    def verify_message(self, pkt):
        if len(pkt) < 40:
            raise HMACError('packet too short')

        sent_digest = pkt[:32]
        msg = pkt[32:]

        nhmac = self.hmac.copy()
        nhmac.update(msg)
        calc_digest = nhmac.digest()

        if not hmac.compare_digest(sent_digest, calc_digest):
            raise HMACError('HMAC signature mismatch')

        sent_timestamp = struct.unpack_from('>Q', msg, 0)[0]
        tsnow = int(time.time() * 1000)
        mintime = tsnow - self.window
        maxtime = tsnow + self.window

        seen = self.timestamps_seen

        # Remove timestamps that are older than the minimum time, since those will already
        # be rejected
        delcnt = 0
        for ts in seen:
            if ts >= mintime:
                break
            delcnt += 1

        if delcnt:
            del seen[:delcnt]

        if sent_timestamp < mintime:
            raise HMACError('Invalid time %d (in past)' % sent_timestamp)

        if sent_timestamp > maxtime:
            raise HMACError('Invalid time %s (in future)' % sent_timestamp)

        if sent_timestamp in seen:
            raise HMACError('Duplicate timestamp (possible replay)')

        # Insert the timestamp into the list, keeping it sorted
        ipnt = len(seen)
        while ipnt > 0 and sent_timestamp < seen[ipnt - 1]:
            ipnt -= 1
        seen.insert(ipnt, sent_timestamp)

        payload = msg[8:]
        return sent_timestamp, payload
