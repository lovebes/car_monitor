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

BATTERY_COMPLAIN_THRESHOLD = 11.5

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

def tire_press_psi(v):
    return int(v * 5.80152) / 10

def callme(*a, **kw):
    sound = kw.pop('sound', 'volt')
    print(a)
    subprocess.call(['callme', '-u', 'volt', '-s', sound] + list(a))

def field_def(self, fld, dval):
    if not hasattr(self, fld):
        setattr(self, fld, dval)

def init(self):
    print('init called')
    field_def(self, 'precondition', False)
    field_def(self, 'charge_complete', False)
    field_def(self, 'lock', False)

    self.last_battery_warn = 0

def handle_packet(self, timestamp, pkt):
    jsf = 'current.json'

    if len(pkt) == 44:
        (sampletime, battery, chargewh, totalwh, evrange,
         plug_status, ac_volts, ac_amps,
         cs_dow, cs_hr, cs_min, ccom_dow, ccom_hr, ccom_min,
         fuel, airtemp, coretemp, oil_life, tire_ft_lf,
         tire_rr_lf, tire_ft_rt, tire_rr_rt, flags, odo, lat, lon, bat_volt) = struct.unpack_from('>IBHIBBBBBBBBBBBBBBBBBBBIiiH', pkt, 0)
    elif len(pkt) == 42:
        (sampletime, battery, chargewh, totalwh, evrange,
         plug_status, ac_volts, ac_amps,
         cs_dow, cs_hr, cs_min, ccom_dow, ccom_hr, ccom_min,
         fuel, airtemp, coretemp, oil_life, tire_ft_lf,
         tire_rr_lf, tire_ft_rt, tire_rr_rt, flags, odo, lat, lon) = struct.unpack_from('>IBHIBBBBBBBBBBBBBBBBBBBIii', pkt, 0)
        bat_volt = 0
    elif len(pkt) == 30:
        odo = lat = lon = 0
        bat_volt = 0
        (sampletime, battery, chargewh, totalwh, evrange,
         plug_status, ac_volts, ac_amps,
         cs_dow, cs_hr, cs_min, ccom_dow, ccom_hr, ccom_min,
         fuel, airtemp, coretemp, oil_life, tire_ft_lf,
         tire_rr_lf, tire_ft_rt, tire_rr_rt, flags) = struct.unpack_from('>IBHIBBBBBBBBBBBBBBBBBBB', pkt, 0)
    else:
        self.log([b2a_hex(pkt).decode('ascii')])
        return

    callme_txt = []

    timetxt = strtime(sampletime + 1500000000)
    data = OrderedDict()
    data['sendtime'] = strtime(timestamp // 1000)
    data['flags'] = flags
    data['time'] = timetxt
    data['coretemp'] = coretemp - 40
    data['airtemp'] = int((airtemp / 2 - 40) * 18 + 320) / 10
    data['plug_status'] = plug_status
    data['ac_volts'] = ac_volts * 2
    data['ac_amps'] = ac_amps / 5
    data['charge_st'] = [cs_dow, cs_hr, cs_min]
    data['charge_comp'] = [ccom_dow, ccom_hr, ccom_min]
    data['battery'] = int(battery * 1000 / 255) / 10
    data['chargekwh'] = chargewh / 1000
    data['totalkwh'] = totalwh / 1000
    data['evrange'] = evrange
    data['fuel'] = int(fuel * 1000 / 255) / 10
    data['oil'] = int(oil_life * 1000 / 255) / 10
    data['tires'] = [tire_press_psi(v) for v in (tire_ft_lf, tire_rr_lf, tire_ft_rt, tire_rr_rt)]
    data['odo'] = odo
    data['lat'] = lat / 3600000
    data['lon'] = lon / 3600000
    data['battvolt'] = auxv = bat_volt / 100
    txt = json.dumps(data)

    self.log([txt])

    with open(jsf + '~', 'w') as fp:
        fp.write(txt)
    os.rename(jsf + '~', jsf)

    callme_sound = 'volt'

    if chargewh == 0:
        self.charge_complete = False
    elif battery >= 250:
        if not self.charge_complete:
            self.charge_complete = True
            callme_txt.append('charge complete, range = %d' % evrange)

    new_lock = bool(flags & 8)
    if self.lock != new_lock:
        self.lock = new_lock
        callme_txt.append('lock status: %d' % (self.lock))

    new_precond = bool(flags & 4)
    if new_precond != self.precondition:
        self.precondition = new_precond
        if self.precondition:
            callme_txt.append('precond started')
        else:
            if not (flags & 2):
                callme_txt.append('precond stopped')

    if 0 < auxv < BATTERY_COMPLAIN_THRESHOLD:
        wtime = time.time()
        need_warn = False
        if wtime > self.last_battery_warn + 550:
            self.last_battery_warn = wtime
            need_warn = True

        if need_warn or callme_txt:
            callme_sound = 'die'
            callme_txt.append('auxv = %.1f!!' % auxv)

    if callme_txt:
        callme('; '.join(callme_txt), sound=callme_sound)
