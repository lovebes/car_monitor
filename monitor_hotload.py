import sys
import re
import time
import os
import array
import termios
import fcntl

import serial
import select
import socket
import argparse
import subprocess
import traceback
import datetime
import ctypes
import json
import gzip
import ctypes
import math
import struct
import threading

from collections import defaultdict, deque, OrderedDict, namedtuple

from os.path import dirname, basename, join, exists
from binascii import b2a_hex

from cardata_shmem import ShareableStructure, CarData

from utils import crc16, getmtime, setup_gpio, set_gpio, get_iface_address, CONFIG, load_config, HMACHelper
from utils import setup_pwm, set_pwm_enable, set_pwm_freq

from importlib import reload

import hud_shm
import i2c_shmem

from hud_shm import *

TEMP_PATH = '/sys/class/thermal/thermal_zone0/temp'

CRIT_VOLT_THRESHOLD = 11.6
IDLE_KILL_TIMEOUT = 4 * 30
ACTIVE_KILL_TIMEOUT = 4 * 120

INFO_PACKET_INTERVAL = 600

LITERS_PER_GAL = 3.785411784
FUEL_CONVERSION = 32768 * LITERS_PER_GAL

VFLAG_DISPLAY_ON = 1
VFLAG_VEHICLE_ON = 2
VFLAG_TEXT_ENTRY = 4
VFLAG_OVERLAY = 8

VFLAG_MENU = 0x10
VFLAG_MUSIC = 0x20
VFLAG_MEDIA_INTERFACE = 0x40

VFLAG_DUMP_PNG = 0x80000000

FLAG_POWER_ON = 1
FLAG_KEY_ON = 2
FLAG_PRECONDITIONING = 4
FLAG_LOCK = 8

STATE_PARKED, STATE_STOPPED, STATE_STOPPING, STATE_MOVING = range(4)

LAST_FULL_ODO_PATH = 'last-full-odo'

# Distances/speeds appear to be in KM, in 6-bit fixed-point values; 64 * 1.609344 = 102.998016
DISTANCE_CONVERSION = 102.998016

SPEED_THRESHOLD_MOVING = int(7.0 * DISTANCE_CONVERSION)
SPEED_THRESHOLD_STOPPING = int(5.0 * DISTANCE_CONVERSION)
SPEED_THRESHOLD_STOPPED = int(0.05 * DISTANCE_CONVERSION)

HVKW_CONV = -64*20*1000
MOTOR_KW_CONV = -100*20*1000

BUTTON_RELEASE = 0x40
BUTTON_ROTOR = 0x80
BUTTON_LONG_PRESS = 0x20

BUTTON_YELLOW = 0
BUTTON_PURPLE = 1
BUTTON_BLUE = 2
BUTTON_GREEN = 3
BUTTON_PINK = 4

BUTTON_CLR_YELLOW = 0xFFF3FF00
BUTTON_CLR_PURPLE = 0xFF9600C7
BUTTON_CLR_BLUE = 0xFF41A7FF
BUTTON_CLR_GREEN = 0xFF31FF36
BUTTON_CLR_PINK = 0xFFFF52CF

FLAG1_LEFT_TURN = 0x000004
FLAG1_RIGHT_TURN = 0x000008
FLAG1_TAILLIGHTS = 0x400000
FLAG1_BRIGHT = 0x800000
FLAG1_HEADLIGHT = 0x100000

BRAKE_OFF = 0
BRAKE_ON = 1
BRAKE_ON_LSIG = 2
BRAKE_ON_RSIG = 3

DEFAULT_ROTOR = 'VOL'

def onstar(k):
    return 'S21024e0973%sff' % k

F241 = 'd007AE'

KEY_COMMANDS = {
    'lock': onstar('0001'),
    'unlock': onstar('0002'),
    'unlockall': onstar('0003'),
    'hatch': onstar('0200'),
    'panicon': onstar('3c00'),
    'panicoff': onstar('1400'),
    'pcon': [onstar('0001'), None, onstar('8001'), None, None, onstar('0000')],
    'pcoff': [onstar('4001'), None, None, onstar('0000')],
    'wipe': F241 + '038000030000',
    'wash': F241 + '038808030000',
    'down': 'w1F0C00',
    'up': 'w2F0C00',
    'downf': 'w130C00',
    'upf': 'w230C00',
    'crack': 'w1F0110',
    'crackf': 'w130110',
}

DIAG_REGISTERS = {
    'high':      (0x02, 0x0000020000, 0x0000020200),
    'rev':       (0x02, 0x0000080000, 0x0000080800),
    'rfog':      (0x02, 0x0000100000, 0x0000101000),
    'cbrk':      (0x02, 0x0000200000, 0x0000202000),
    'lfsig':     (0x02, 0x1000000000, 0x1010000000),
    'lrsig':     (0x02, 0x2000000000, 0x2020000000),
    'rfsig':     (0x02, 0x4000000000, 0x4040000000),
    'rrsig':     (0x02, 0x8000000000, 0x8080000000),

    'lhead':     (0x06, 0x0100000000, 0x017FFF0000),
    'rhead':     (0x06, 0x0200000000, 0x0200007FFF),

    'lpark':     (0x07, 0x0100000000, 0x017FFF0000),
    'rpark':     (0x07, 0x0200000000, 0x0200007FFF),

    'interior':  (0x08, 0x0100000000, 0x017FFF0000),

    'led':       (0x09, 0x0200000000, 0x0200007FFF),

    'inadvload': (0x0C, 0x0100000000, 0x0101000000),

    'lp':        (0x0F, 0x0400000000, 0x0404000000),
    'ldrl':      (0x0F, 0x4000000000, 0x4040000000),
    'rdrl':      (0x0F, 0x8000000000, 0x8080000000),

    'charger':   (0x14, 0x0000020000, 0x0000020002),

    'cargo':     (0x1D, 0x0100000000, 0x017FFF0000),
}

DIAG_COMBOS = {
    'tail': ['lrsig', 'rrsig'],
    'head': ['lhead', 'rhead'],
    'drl': ['ldrl', 'rdrl'],
    'brk': ['lrsig', 'rrsig', 'cbrk'],
    'lsig': ['lrsig', 'lfsig'],
    'rsig': ['rrsig', 'rfsig'],
    'ext': ['rev', 'rfog', 'cbrk', 'lfsig', 'lrsig', 'rfsig', 'rrsig', 'lhead', 'rhead', 'lpark', 'rpark', 'ldrl', 'rdrl'],
}

IDLE_QUERIES = []

FT_INVALID, FT_DATA, FT_EVENT, FT_REPLY, FT_TCODE, FT_OBD, FT_PTMSG = range(7)

(EV_PWROFF, EV_PWRON, EV_DCOFF, EV_DCON, EV_CCUP, EV_CCDN, EV_CCPWR, EV_CCCANCEL,
 EV_VOLUP, EV_VOLDN, EV_TRACKUP, EV_TRACKDN, EV_SRC, EV_VOICE, EV_MUTE,
 EV_DCCMDOFF, EV_DCCMDON, EV_BUS_INACTIVE, EV_BUS_ACTIVE, EV_KEYOFF, EV_KEYON,
 EV_UNLOCK, EV_LOCK) = range(23)

EVENT_NAMES = [
    "PWROFF",
    "PWRON",
    "DCOFF",
    "DCON",
    "CCUP",
    "CCDN",
    "CCPWR",
    "CCCANCEL",
    "VOLUP",
    "VOLDN",
    "TRACKUP",
    "TRACKDN",
    "SRC",
    "VOICE",
    "MUTE",
    "DCCMDOFF",
    "DCCMDON",
    "BUS_INACTIVE",
    "BUS_ACTIVE",
    "KEYOFF",
    "KEYON",
    "UNLOCK",
    "LOCK"
]

GPIO_CPU_FAN = 21

CPU_TEMP_THRES = 50000

epoch = datetime.datetime(1970, 1, 1, 0, 0, 0)

NOTEMUL = {
    'c': 2**(-9/12),
    'c#': 2**(-8/12),
    'db': 2**(-7/12),
    'd': 2**(-7/12),
    'd#': 2**(-6/12),
    'eb': 2**(-6/12),
    'e': 2**(-5/12),
    'f': 2**(-4/12),
    'f#': 2**(-3/12),
    'gb': 2**(-3/12),
    'g': 2**(-2/12),
    'g#': 2**(-1/12),
    'ab': 2**(-1/12),
    'a': 1.0,
    'a#': 2**(1/12),
    'bb': 2**(1/12),
    'b': 2**(2/12)
}

def makebeep(txt):
    outarr = []
    stack = []
    duration = 150
    pw = 100
    octave = 4
    for ins in txt.lower().split():
        notelen = 1.0
        if not ins:
            continue

        if ins == '[':
           stack.push((duration, pw, octave))

        if ins == ']':
           duration, pw, octave = stack.pop()

        if ins.startswith('='):
            duration = int(ins[1:])
            continue

        if ins.startswith('*'):
            pw = int(ins[1:])
            continue

        noteoct = octave
        while ins.startswith('<'):
            noteoct -= 1
            ins = ins[1:]
        while ins.startswith('>'):
            noteoct += 1
            ins = ins[1:]

        if len(ins) > 1:
            if ins.endswith('e'):
                notelen = 0.5
                ins = ins[:-1]
            elif ins.endswith('w'):
                notelen = 4.0
                ins = ins[:-1]
            elif ins.endswith('h'):
                notelen = 2.0
                ins = ins[:-1]
            elif ins.endswith('q'):
                notelen = 1.0
                ins = ins[:-1]

        if not ins:
            octave = noteoct
            continue

        curdur = int(duration * notelen)
        silence_len = int((100 - pw) * duration / 100)
        if ins == '!':
           outarr.append(0)
           outarr.append(curdur)
        else:
            freqmul = NOTEMUL.get(ins)
            if freqmul:
                freq = int(110 * freqmul * (1 << noteoct))
                outarr.append(freq)
                outarr.append(curdur - silence_len)
                if silence_len:
                    outarr.append(0)
                    outarr.append(silence_len)

    return outarr


####################################################################################
# Registration decorators

def regwrap(rf):
    def wrap(*args, **kw):
        def dec(f):
            rf(f, *args, **kw)
            return f
        return dec
    return wrap

button_mode = defaultdict(lambda: ({}, {}, {}))
@regwrap
def button(f, btn, longpress=False, mode='default', beep=(200, 5)):
    button_mode[mode][1 if longpress else 0][btn] = f, beep

@regwrap
def rotor(f, btn, beep=1000, mode='default', **kw):
    button_mode[mode][2][btn] = f, beep

event_handlers = {}
@regwrap
def event(f, ev):
    event_handlers[ev] = f

reply_handlers = {}
@regwrap
def reply(f, ch):
    reply_handlers[ch] = f

message_handlers = {}
@regwrap
def msg(f, ev):
    message_handlers[ev] = f

menu_items = []
@regwrap
def menu(f, text, key=''):
    menu_items.append((f, text, key))

# Registration decorators
####################################################################################

####################################################################################
# Init

class MenuItem:
    def __init__(self, func, text, key):
        self.text = text
        self.func = func
        self.key = key

def init(self):
    load_config()

    self.next_time_check = 0

    self.last_rotor = 0
    self.expect_seq = -1
    self.rotor_rawval = 0

    self.menu_pos = -1

    self.last_cardata = CarData()
    self.cardata = CarData.create('/dev/shm/cardata')
    self.cardata.fw_millis = 0
    self.last_cardata.fw_millis = 0

    self.widget_config = wc = WidgetConfig.from_mmap('/dev/shm/hud')
    self.cur_button_mode = 'default'

    self.logger = None

    init_widgets(self)
    self.textent_column = 0

    self.menu_items = [MenuItem(f, text, key) for f, text, key in menu_items]

    bykey = self.menu_by_key = {}
    for item in self.menu_items:
        if item.key:
            bykey[item.key] = item

    self.overlay_expire = 0

    self.cur_button_press = (0, False)


    self.cur_text = ''
    self.text_pos = 0

    self.fanspeed_target = FanSpeedTarget()
    self.temp_target = TemperatureTarget()

    self.bluetooth_timeout = 0
    self.display_timeout = 0
    self.display_power = None
    self.display_power_lockout = 0

    self.reboot_confirm_time = 0

    self.volume_count = 0

    self.idle_queries = [cls() for cls in IDLE_QUERIES]
    self.idle_query_len = sum(cls.nbytes for cls in IDLE_QUERIES)
    self.iq_next_start = 0
    self.iq_last_start = 0
    self.iq_last_wakeup = 0
    self.iq_index = -1
    self.iq_data_packet = b''
    self.iq_data = None
    self.iq_data_time = 0
    self.info_hmac = HMACHelper(CONFIG['info_hmac'])

    self.next_info_packet = 0

    self.delay_query_queue = deque()
    self.time_queue = deque()

    fn = 'monitor_config.json'
    try:
        with open(join(dirname(__file__), fn)) as fp:
            self.config = json.load(fp)
    except Exception:
        self.config = {}

    field_def(self, 'precondition_wait_time', None)
    field_def(self, 'preconditioning', False)
    field_def(self, 'bus_active', False)
    field_def(self, 'climate_active', False)
    field_def(self, 'climate_eco', False)
    field_def(self, 'climate_fan_double', False)
    field_def(self, 'climate_recirc', False)
    field_def(self, 'climate_recirc_time', 0)

    field_def(self, 'force_connect', False)
    field_def(self, 'key_on', False)
    field_def(self, 'vehicle_on', False)
    field_def(self, 'display_active', False)
    field_def(self, 'odo', OdoRecalc())

    field_def(self, 'motion_state', 0)
    field_def(self, 'total_time', 0)
    field_def(self, 'total_stop_time', 0)
    field_def(self, 'cur_stop_time', 0)
    field_def(self, 'last_stop_time', 0)
    field_def(self, 'cur_fw_millis', 0)

    field_def(self, 'diag_lights', {})
    field_def(self, 'last_diag_light_send', 0)

    field_def(self, 'last_temp_log_time', 0)

    field_def(self, 'is_charged', False)

    field_def(self, 'force_canlog', False)
    field_def(self, 'canlog_source_process', None)
    field_def(self, 'canlog_sink_process', None)
    field_def(self, 'canlog_sent_kill', False)
    field_def(self, 'engine_hack_active', False)

    field_def(self, 'brake_light_state', 0)

    field_def(self, 'last_range', -1)
    field_def(self, 'last_range_odo', 0)

    field_def(self, 'lock', False)

    field_def(self, 'next_high_level', 0)
    field_def(self, 'old_fanspeed', 0)

    field_def(self, 'media_interface_data', None)

    field_def(self, 'range_samples', deque())

    field_def(self, 'music_data', ('', '', '', '', '', False))

    field_def(self, 'volt_sense', None)
    field_def(self, 'bat_voltage', 0)
    field_def(self, 'bat_current', 0)

    field_def(self, 'last_lat', 0)
    field_def(self, 'last_lon', 0)

    field_def(self, 'gear_mismatch_count', 0)
    field_def(self, 'last_swcan_warn', 0)

    field_def(self, 'cpu_fan_on_time', 0)

    field_def(self, 'last_mode_switch', 0)
    field_def(self, 'last_mode_dir', 0)

    field_def(self, 'beeper', None)

    self.i2c_data = i2c_shmem.I2CData.create(i2c_shmem.PATH)
    self.panic_kill_timer = 0


    update_music(self)

    self.auto_resume = False

    self.trip_dtc_seen = set()

    self.wjt_cmodesel.update(self.climate_eco)

    self.debug_monitor_pid = None

    self.last_full_odo = 0
    try:
        with open(LAST_FULL_ODO_PATH, 'r') as fp:
            self.last_full_odo = float(fp.read().strip())
    except (IOError, ValueError):
        pass

    setup_gpio(GPIO_CPU_FAN)
    setup_pwm(0)

    if self.beeper is not None:
        self.beeper.stop()

    self.beeper = Beeper()
    self.beeper.start()


    bd = 150
    beep = makebeep('g >c e f')
    print(beep)
    self.beeper.beepm(beep)

def field_def(self, fld, dval):
    if not hasattr(self, fld):
        setattr(self, fld, dval)

def init_widgets(self):
    wc = self.widget_config
    wc.build(all_widgets, 800, 480)

    self.wjt_music_title = wc.by_key['MusicTitleWidget']
    self.wjt_music_artist = wc.by_key['MusicArtistWidget']
    self.wjt_music_time = wc.by_key['MusicTimeWidget']

    self.wjt_cmodesel = wc.by_key['ClimateModeSelWidget']
    self.wjt_textent = wc.by_key['TextEntryWidget']
    self.wjt_textentcurs = wc.by_key['TextEntryCursorWidget']
    self.wjt_overlay = wc.by_key['OverlayWidget']
    self.wjt_clock = wc.by_key['ClockWidget']
    self.wjt_batvolt = wc.by_key['BattVoltWidget']
    self.wjt_batcurrent = wc.by_key['BattCurrentWidget']
    self.wjt_record_state = wc.by_key['RecordStateWidget']
    self.wjt_connect_state = wc.by_key['ConnectStateWidget']
    self.wjt_temperature = wc.by_key['CoreTemperatureWidget']
    self.wjt_debugpid = wc.by_key['DebugPidWidget']

    self.wjt_media_intf_header = wc.by_key['MediaInterfaceHeader']
    self.wjt_media_intf_line1 = wc.by_key['MediaInterfaceLine1']
    self.wjt_media_intf_line2 = wc.by_key['MediaInterfaceLine2']
    self.wjt_media_intf_btn1 = wc.by_key['MediaInterfaceButton1']
    self.wjt_media_intf_btn2 = wc.by_key['MediaInterfaceButton2']
    self.wjt_media_intf_btn3 = wc.by_key['MediaInterfaceButton3']

    w = self.textent_widgets = []
    for col in range(11):
        lst = []
        w.append(lst)
        for row in range(4):
            lst.append(wc.by_key[row, col])

    self.cardata_widgets = [w for w in wc.widgets if w.update_from_data]
    self.wjt_menu = [wc.by_key['menu', j] for j in range(12)]

# Init
####################################################################################

####################################################################################
# Misc util functions

def hms(secs):
    mins = secs / 60
    secs %= 60
    if mins >= 90:
        hrs = mins / 60
        mins %= 60
        return '%d:%02d:%02d' % (hrs, mins, secs)
    else:
        return '%d:%02d' % (mins, secs)

def sendq(self, q):
    #self.log('query: %r' % q)
    self.sendq(q)

def distance_to_db(val):
    return int(val * 1000.0 + 0.5)

def send_key_command(self, cmd):
    print('key command: %r' % cmd)
    try:
        val = KEY_COMMANDS[cmd]
        if isinstance(val, str):
            self.delay_query_queue.append(val)
        else:
            self.delay_query_queue.extend(val)
        check_delay_queue(self)
    except KeyError:
        print('%r is invalid' % cmd)
        pass

BATT_LOW = 0
BATT_MID = 51
BATT_HIGH = 230

def batt_range(raw_battery):
    pct = raw_battery
    if raw_battery < BATT_MID:
        return (pct - BATT_LOW) * 100.0 / (BATT_MID - BATT_LOW), False
    else:
        return (pct - BATT_MID) * (100.0 / (BATT_HIGH - BATT_MID)), True

def do_shutdown(self):
    self.beeper.stop()
    self.beeper.join()
    os.system('./do-shutdown')

# Misc util functions
####################################################################################

####################################################################################
# Motion State

def clear_motion_state(self):
    self.total_time = 0
    self.total_stop_time = 0
    self.cur_stop_time = 0
    self.last_log_time = 0
    self.last_stop_time = 0

def set_motion_state(self, state):
    if self.motion_state == state:
        return

    if self.motion_state == STATE_STOPPED:
        self.last_stop_time = self.cur_stop_time
        self.total_stop_time += self.cur_stop_time

    self.motion_state = state

    if state != STATE_STOPPED and state != STATE_STOPPING:
        self.cur_stop_time = 0

def update_motion_state(self, timediff, gear, speed):
    if self.motion_state != STATE_PARKED:
        self.total_time += timediff
        if self.motion_state == STATE_STOPPING or self.motion_state == STATE_STOPPED:
            self.cur_stop_time += timediff

    if gear == 0:
        set_motion_state(self, STATE_PARKED)
    else:
        if self.motion_state == STATE_PARKED:
            set_motion_state(self, STATE_STOPPED)

        if speed > SPEED_THRESHOLD_MOVING:
            set_motion_state(self, STATE_MOVING)
        elif speed < SPEED_THRESHOLD_STOPPING:
            if self.motion_state == STATE_MOVING:
                set_motion_state(self, STATE_STOPPING)

            if speed < SPEED_THRESHOLD_STOPPED:
                set_motion_state(self, STATE_STOPPED)

# Motion State
####################################################################################

####################################################################################
# Idle Queries

def iq(cls):
    IDLE_QUERIES.append(cls)
    return cls

class IdleQuery:
    name = ''
    module = 0
    pid = 0
    nbytes = 1

    def parse_raw_val(self, sm, va, vb, vc, vd):
        return va

    def format_val(self, rv):
        return ''

    def encode_val(self, rv):
        return bytes([rv])

@iq
class BatterySOC(IdleQuery):
    name = 'BATTERY_SOC'
    module = 4
    pid = 0x8334
    nbytes = 1

    def parse_raw_val(self, sm, va, vb, vc, vd):
        return va

    def format_val(self, rv):
        return '%.1f%%' % (rv / 2.55)

    def encode_val(self, rv):
        return bytes([rv])

@iq
class ChargeWH(IdleQuery):
    name = 'CHARGE_WH'
    module = 4
    pid = 0x437d
    nbytes = 6

    def parse_raw_val(self, sm, va, vb, vc, vd):
        wh = (va*256 + vb) * 10

        path = 'charge_wh.json'
        try:
            with open(path, 'r') as fp:
                data = json.load(fp, object_pairs_hook=OrderedDict)
        except (ValueError, FileNotFoundError):
            data = OrderedDict()
        last_wh = data.get('current_wh', 0)
        total_wh = data.get('total', 0)
        if wh < last_wh:
            delta = wh
        else:
            delta = wh - last_wh

        total_wh += delta
        data['current_wh'] = wh
        data['total'] = total_wh
        with open(path + '~', 'w') as fp:
            json.dump(data, fp, indent=True)
            fp.write('\n')
        os.rename(path + '~', path)
        return wh, total_wh

    def format_val(self, rv):
        return '%.2f kWh (%.2f kWh)' % (rv[0] / 1000, rv[1] / 1000)

    def encode_val(self, rv):
        return struct.pack('>HI', rv[0], rv[1])

@iq
class Range(IdleQuery):
    name = 'RANGE'
    module = 4
    pid = 0x41a6
    nbytes = 1

    def parse_raw_val(self, sm, va, vb, vc, vd):
        return int((va*256 + vb) / DISTANCE_CONVERSION + 0.1)

    def format_val(self, rv):
        return '%d mi' % rv

    def encode_val(self, rv):
        return bytes([max(0, min(255, rv))])

@iq
class PlugStatus(IdleQuery):
    name = 'PLUG_STATUS'
    module = 4
    pid = 0x43CA

    def format_val(self, rv):
        return '%d' % (rv)

@iq
class ACVolts(IdleQuery):
    name = 'AC_VOLTS'
    module = 4
    pid = 0x4368

    def format_val(self, rv):
        return '%d V' % (rv * 2)

@iq
class ACAmps(IdleQuery):
    name = 'AC_AMPS'
    module = 4
    pid = 0x4369

    def format_val(self, rv):
        return '%.1f A' % (rv / 5)

@iq
class ChargeStart(IdleQuery):
    name = 'CHARGE_START'
    module = 4
    pid = 0x83e1
    nbytes = 3

    DOW = ['???', 'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
    def parse_raw_val(self, sm, va, vb, vc, vd):
        return va, vb, vc

    def format_val(self, rv):
        if rv == (1, 0, 0):
            return '---'

        try:
            dow_text = self.DOW[rv[0]]
        except IndexError:
            dow_text = '%s' % rv[0]
        return '%s %02d:%02d' % (dow_text, rv[1], rv[2])

    def encode_val(self, rv):
        return bytes(rv)

@iq
class ChargeComplete(ChargeStart):
    name = 'CHARGE_COMP'
    module = 4
    pid = 0x83e2

@iq
class Fuel(IdleQuery):
    name = 'FUEL'
    module = 0
    pid = 0x2F
    nbytes = 1

    def parse_raw_val(self, sm, va, vb, vc, vd):
        return va

    def format_val(self, rv):
        return '%.1f%%' % (rv / 2.55)

    def encode_val(self, rv):
        return bytes([rv])

# Idle Queries
####################################################################################

####################################################################################
# Climate controls

class ValueTarget:
    field = ''
    def __init__(self):
        self.target = None
        self.last_send = 0
        self.retry_count = 0

    def check_time(self, mon, cmontime):
        if self.target is not None and cmontime > self.last_send + 0.5:
            if self.retry_count:
                print('retry %s' % type(self).__name__)
                self.retry_count -= 1
                self.move_to_target(mon, 0)
            else:
                self.target = None

    def move_to_target(self, mon, curdelta):
        target = self.target

        if target is not None:
            self.last_send = getmtime()

            curval = self.convert(getattr(mon.last_cardata, self.field))
            diff = (target - curval)

            if diff == 0 or (diff > 0 and curdelta < 0) or (diff < 0 and curdelta > 0):
                self.target = None
            else:
                if diff > 0:
                    sendq(mon, self.upq(curval))
                else:
                    sendq(mon, self.dnq(curval))

    def get_current_val(self, mon):
        val = self.target
        if val is None:
            val = self.convert(getattr(mon.last_cardata, self.field))
        return val

    def set_target(self, mon, newval):
        need_send = self.target is None

        self.last_send = getmtime()

        self.retry_count = 6

        if not self.min_value <= newval <= self.max_value:
            return False

        self.target = newval
        if need_send:
            self.move_to_target(mon, 0)
        return True

    def adjust_target(self, mon, up):
        delta = 1 if up else -1
        newval = self.get_current_val(mon) + delta
        return self.set_target(mon, newval)

class FanSpeedTarget(ValueTarget):
    min_value = 0
    max_value = 8
    field = 'select_fanspeed'
    def upq(self, curval):
        if curval == 0:
            return 'A9'
        else:
            return 'A23'

    def dnq(self, curval):
        return'A22'

    @staticmethod
    def convert(rv):
        return rv & 0xF

class TemperatureTarget(ValueTarget):
    min_value = 60
    max_value = 90

    field = 'select_temp'
    def upq(self, curval):
        return 'A30'

    def dnq(self, curval):
        return 'A31'

    @staticmethod
    def convert(rv):
        if rv == 31:
            return 60
        elif rv == 32:
            return 90
        else:
            return 61 + rv

def check_recirc_mode(self):
    recirc_active = (self.cardata.recirc & 1)
    if recirc_active != self.climate_recirc:
        ctime = getmtime()
        if (ctime - self.climate_recirc_time) > 2:
            self.climate_recirc_time = ctime
            sendq(self, 'A16')
    else:
        self.climate_recirc_time = 0

# Climate controls
####################################################################################


def find_macchina():
    basepath = '/sys/class/tty'

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
            if vendor == '2341' and product == '003e':
                return dev
        except IOError:
            pass

####################################################################################
# Beeper

class Beeper(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._have_queue = threading.Condition(self._lock)
        self._want_stop = False
        self._queue = deque()
        self._enabled = False

    def _enable_pwm(self, val):
        if val != self._enabled:
            set_pwm_enable(0, val)
            self._enabled = val

    def _get_queue(self):
        with self._lock:
            while not self._queue and not self._want_stop:
                self._enable_pwm(False)
                self._have_queue.wait()

            if self._want_stop:
                return None

            return self._queue.popleft()

    def stop(self):
        with self._lock:
            self._want_stop = True
            self._have_queue.notify()

    def do_beep(self, freq):
        if freq == 0:
            self._enable_pwm(False)
        else:
            set_pwm_freq(0, freq)
            self._enable_pwm(True)

    def run(self):
        set_pwm_enable(0, False)
        try:
            while True:
                data = self._get_queue()
                if data is None:
                    return
                freq, dur = data
                self.do_beep(freq)
                time.sleep(dur / 1000)
        finally:
            set_pwm_enable(0, False)

    def addqueue(self, pattern):
        with self._lock:
            notify = not self._queue
            self._queue.extend(pattern)
            if notify:
                self._have_queue.notify()


    def beepm(self, lst):
        self.addqueue(zip(lst[::2], lst[1::2]))

    def beep(self, freq, dur):
        self.addqueue([(freq, dur)])

# Beeper
####################################################################################

class CarDataLogger:
    LOG_VERS = 3

    row_order = [
        #AUTO START : monitor_hotload CarDataLogger row_order
        'wrc3',
        'wrc2',
        'wrc1',
        'hv_amps',
        'hv_volts',
        'mga_rpm',
        'mga_amps',
        'mga_volts',
        'mgb_rpm',
        'mgb_amps',
        'mgb_volts',
        'rawspeed',
        'steer',
        'brake_pct',
        'accel_pct',
        'range',
        'rpm',
        'fuel_ctr',
        'battery_soc',
        'battery_raw_soc',
        'motion_state',
        'gear',
        'scflags',
        'clutch_state',
        'raw_odometer',
        'coolant_temp',
        'intake_temp',
        'battery_temp',
        'lat',
        'lon',
        'air_temp1',
        'air_temp2',
        'air_pressure',
        'vent',
        'select_fanspeed',
        'select_temp',
        'recirc',
        'climate_mode',
        'climate_power',
        'tire_ft_lf',
        'tire_rr_lf',
        'tire_ft_rt',
        'tire_rr_rt',
        'heat_ac',
        'rear_defrost',
        'fanspeed',
        'rawccspeed',
        'ccbtn',
        'radiobtn',
        'drive_mode',
        'oil_life',
        #AUTO END
    ]

    def __init__(self, logdir):
        self.logdir = logdir
        try:
            os.makedirs(logdir)
        except OSError:
            pass

        self.last_flush_time = 0
        self.last_fw_millis = 0
        self.last_log_time = 0
        self.logfile = None
        self.writer = None
        self.need_full_update = True

    def delta_time(self, ctime):
        delta = ctime - self.last_log_time
        self.last_log_time = ctime
        return delta

    def delta_fwtime(self, ctime):
        delta = ctime - self.last_fw_millis
        self.last_fw_millis = ctime
        return delta

    def open_log(self):
        self.last_log_time = 0
        self.last_fw_millis = 0
        self.last_flush_time = int(getmtime() * 1000)

        timestr = time.strftime('%F__%H-%M-%S', time.localtime(time.time()))
        self.logfile = join(self.logdir, timestr + '.txt.gz')
        self.base_time = None
        self.writer = gzip.open(self.logfile, 'wt')
        cmtime = int(getmtime() * 1000)
        self.write_row(cmtime, 'V', [str(self.LOG_VERS)])
        self.write_row(cmtime, 'F', ['fw_millis'] + self.row_order)
        self.write_timesync(int(getmtime() * 1000))
        try:
            with open('record_start_time') as fp:
                rstarttime = float(fp.readline().strip())
            self.write_row(cmtime, 'C', [str(int(rstarttime * 1000))])
        except (IOError, ValueError, EOFError):
            pass

    def write_timesync(self, ctime):
        w = self.writer
        if w is None:
            return
        dt = self.delta_time(ctime)
        w.write('%d\tW\t%d\n' % (dt, int(time.time() * 1000)))

    def write_row(self, cmtime, typ, row):
        w = self.writer
        if w is None:
            return

        dt = self.delta_time(cmtime)

        w.write('%d\t%s\t' % (dt, typ))
        w.write('\t'.join(row))
        w.write('\n')

        if cmtime >= self.last_flush_time + 10000:
            self.write_timesync(cmtime)
            w.flush()
            self.last_flush_time = cmtime

    def close_log(self):
        self.write_timesync(int(getmtime() * 1000))
        self.writer.close()
        self.writer = None
        self.logfile = None

    def log_data_frame(self, cmtime, fw_millis, cd, lcd):
        if self.writer is None:
            return

        row = [str(self.delta_fwtime(fw_millis))]

        if self.need_full_update:
            row.extend(str(getattr(cd, field)) for field in self.row_order)
            self.need_full_update = False
        else:
            for field in self.row_order:
                lv = getattr(lcd, field)
                cv = getattr(cd, field)
                row.append('' if lv == cv else str(cv))
        self.write_row(cmtime, 'D', row)

    def log_event(self, cmtime, fw_millis, etype):
        try:
            ename = EVENT_NAMES[etype]
        except IndexError:
            ename = 'EVENT_%d' % etype
        self.write_row(cmtime, 'E', (str(self.delta_fwtime(fw_millis)), ename))

    def log_marker(self, text):
        self.write_row(int(getmtime() * 1000), 'M', (text,))

    def log_gps(self, data):
        self.write_row(int(getmtime() * 1000), 'G', [('' if v is None else str(v)) for v in data])

data_query_complete = []


####################################################################################
# Event / Query reply handlers

@reply('T')
def handle_time(self, fwm, bs):
    millis = bs.read_bits(32)
    year = bs.read_bits(7)
    month = bs.read_bits(4)
    day = bs.read_bits(5)
    hour = bs.read_bits(5)
    min = bs.read_bits(6)
    sec = bs.read_bits(6)
    if year != 0 and month != 0:
        walltime = time.time()
        cdate = (datetime.datetime(year + 2000, month, day, hour, min, sec) - epoch).total_seconds() + millis / 1000
        diff = cdate - walltime
        if abs(diff) > 0.01:
            time.clock_settime(time.CLOCK_REALTIME, cdate)
            self.log('set time ofs %.3f' % diff)

def set_bus_active(self, active):
    if active != self.bus_active:
        self.bus_active = active

        ctime = getmtime()
        if active:
            with open('bus-active', 'w') as fp:
                pass
            if ctime >= self.iq_last_start + 30 and self.iq_index == -1:
                #piggyback idle queries when bus goes active
                #start_idle_query(self)
                pass
            check_delay_queue(self)
        else:
            try:
                os.unlink('bus-active')
            except OSError:
                pass

def set_preconditioning(self, precond):
    if self.preconditioning != precond:
        self.preconditioning = precond
        send_info_packet(self)

def set_key_on(self, keyon):
    if keyon != self.key_on:
        self.key_on = keyon
        self.precondition_wait_time = None
        update_active(self, getmtime())

def set_power_on(self, poweron):
    if poweron != self.vehicle_on:
        ctime = getmtime()
        self.vehicle_on = poweron
        set_charge_level(self, True)
        if poweron:
            clear_motion_state(self)
            if not self.key_on:
                self.precondition_wait_time = ctime + 2
            self.odo = OdoRecalc()
            self.trip_dtc_seen = set()
        else:
            self.precondition_wait_time = None
        update_active(self, ctime)

def set_dc_flag(flag, val):
    if val:
        with open(flag, 'w') as fp:
            pass
    else:
        try:
            os.unlink(flag)
        except OSError:
            pass

def set_lock(self, lock):
    if lock != self.lock:
        self.lock = lock
        send_info_packet(self)

@event(EV_DCCMDOFF)
def handle_dccmdoff(self, fwm, bs):
    set_dc_flag('want-record', False)

@event(EV_DCCMDON)
def handle_dccmdon(self, fwm, bs):
    set_dc_flag('want-record', True)

@reply('K')
def handle_key_reply(self, fwm, bs):
    set_power_on(self, bs.read_bits(1))
    set_key_on(self, bs.read_bits(1))
    set_bus_active(self, bs.read_bits(1))

@event(EV_PWRON)
def handle_pwron(self, fwm, bs):
    set_power_on(self, True)

@event(EV_PWROFF)
def handle_pwron(self, fwm, bs):
    set_power_on(self, False)

@event(EV_KEYON)
def handle_keyon(self, fwm, bs):
    set_key_on(self, True)

@event(EV_KEYOFF)
def handle_keyoff(self, fwm, bs):
    set_key_on(self, False)

@event(EV_BUS_ACTIVE)
def handle_bus_active(self, fwm, bs):
    set_bus_active(self, True)

@event(EV_BUS_INACTIVE)
def handle_bus_inactive(self, fwm, bs):
    set_bus_active(self, False)

@event(EV_LOCK)
def handle_lock(self, fwm, bs):
    set_lock(self, True)

@event(EV_UNLOCK)
def handle_unlock(self, fwm, bs):
    set_lock(self, False)

# Query reply handlers
####################################################################################

####################################################################################
# Display / Popup overlay management

def show_overlay(self, txt, time=1.5):
    if time is not None:
        self.overlay_expire = getmtime() + time

    self.wjt_overlay.set_text(txt)
    self.wjt_overlay.cfg = 0xFFFFFF
    self.wjt_overlay.bump_version()
    self.widget_config.set_visgroup(VFLAG_OVERLAY, VFLAG_OVERLAY)

def clear_overlay(self):
    self.overlay_expire = None
    self.widget_config.set_visgroup(VFLAG_OVERLAY, 0)

def check_overlay(self):
    if not self.overlay_expire:
        return
    ctime = getmtime()
    if ctime > self.overlay_expire:
        clear_overlay(self)

def update_active(self, cmontime):
    if self.vehicle_on and self.key_on:
        disp = 'ON', 0xAAFFAA
        self.bluetooth_timeout = cmontime + 15
        self.display_timeout = cmontime + 40
    else:
        if self.force_connect:
            self.display_timeout = cmontime + 10

        rtime = max(0, int(self.display_timeout - cmontime))
        disp = '%d' % rtime, 0xcccccc

    if self.force_connect:
        disp = 'OVR', 0xFFFF88

    if disp != self.wjt_connect_state.lastval:
        self.wjt_connect_state.lastval = disp
        self.wjt_connect_state.set_text(disp[0])
        self.wjt_connect_state.cfg = disp[1]
        self.wjt_connect_state.bump_version()

    display_active = self.force_connect or cmontime < self.display_timeout
    if cmontime >= self.display_power_lockout:
        if display_active != self.display_power:
            self.i2c_data.enable_display(display_active)
            self.display_power = display_active
            self.display_power_lockout = cmontime + 5

    flags = (VFLAG_VEHICLE_ON if self.vehicle_on or self.force_connect else 0) | (VFLAG_DISPLAY_ON if display_active else 0)
    self.widget_config.set_visgroup(VFLAG_VEHICLE_ON | VFLAG_DISPLAY_ON, flags)

    self.set_bluetooth(self.force_connect or cmontime < self.bluetooth_timeout)

    if (self.vehicle_on or self.force_canlog) and get_config_int(self, 'enable_canlog', 0):
        if self.canlog_source_process is None:
            macchina = find_macchina()
            if macchina:
                self.canlog_sent_kill = False
                timestamp = time.strftime('%Y-%m-%d__%H-%M-%S', time.localtime())
                output = join('../canlog', 'canlog-%s.bin.gz' % timestamp)
                shmem_file = join('/dev/shm', 'canlog-shmem-%s' % timestamp)

                print('starting canlog: %s' % output)
                for f in os.listdir('/dev/shm'):
                    if f.startswith('canlog-shmem-'):
                        try:
                            os.unlink(join('/dev/shm', f))
                        except OSError:
                            pass

                self.canlog_source_process = subprocess.Popen(['./canlog_shmem', shmem_file, 'source', '/dev/' + macchina])
                self.canlog_sink_process = subprocess.Popen(['./canlog_shmem', shmem_file, 'sink', output])

    else:
        if self.canlog_source_process is not None:
            if not self.canlog_sent_kill:
                print('stopping canlog')
                self.canlog_source_process.terminate()
                self.canlog_sink_process.terminate()
                self.canlog_sent_kill = True

    if self.vehicle_on:
        if self.logger is None:
            self.cur_fw_millis = 0
            self.logger = CarDataLogger(CONFIG['cardata_path'])
            self.logger.open_log()

    else:
        if self.logger is not None:
            self.logger.close_log()
            self.logger = None

# Display / Popup overlay management
####################################################################################

####################################################################################
# Periodic functions

def tick(self):
    '''Called every 250ms by serial_monitor.py'''

    cmontime = getmtime()
    check_overlay(self)
    if cmontime >= self.next_time_check:
        self.sendq('T')
        self.sendq('K')
        self.next_time_check += 15
        if cmontime >= self.next_time_check:
            self.next_time_check = cmontime + 15

    if (cmontime - self.last_rotor) >= 1:
        self.rotor_rawval = 0


    self.fanspeed_target.check_time(self, cmontime)
    self.temp_target.check_time(self, cmontime)
    check_recirc_mode(self)

    with open('state', 'r') as fp:
        cstate = fp.read().strip()
    #cstate = 'xxx'
    if cstate != self.wjt_record_state.lastval:
        self.wjt_record_state.set_text(cstate)
        self.wjt_record_state.lastval = cstate
        self.wjt_record_state.bump_version()

    if self.canlog_source_process:
        canlog_rc = self.canlog_source_process.poll()
        if canlog_rc is not None:
            print('canlog source exited')
            self.canlog_source_process = None

    if self.canlog_sink_process:
        canlog_rc = self.canlog_sink_process.poll()
        if canlog_rc is not None:
            print('canlog sink exited')
            self.canlog_sink_process = None

    update_active(self, cmontime)

    if self.volume_count > 0:
        sendq(self, 'R1')
        self.volume_count -= 1
    elif self.volume_count < 0:
        sendq(self, 'R2')
        self.volume_count += 1

    with open(TEMP_PATH) as fp:
        temp = int(fp.read().strip())
        self.current_temp = temp // 1000
        temp_txt = '%2d\xb0C' % (self.current_temp)
        wjt = self.wjt_temperature
        if temp_txt != wjt.lastval:
            wjt.lastval = temp_txt
            wjt.set_text(temp_txt)
            wjt.bump_version()

        if cmontime > self.last_temp_log_time + 30:
            self.last_temp_log_time = cmontime
            print('temp = %2d.%03d' % (temp // 1000, temp % 1000))

        if temp >= CPU_TEMP_THRES + 1000:
            if not self.cpu_fan_on_time:
                self.cpu_fan_on_time = cmontime
                #print('fan on!')

            set_gpio(GPIO_CPU_FAN, True)
        elif temp <= CPU_TEMP_THRES - 1000:
            if cmontime >= self.cpu_fan_on_time + 30:
                #if self.cpu_fan_on_time:
                #    print('fan off!')
                self.cpu_fan_on_time = 0
                set_gpio(GPIO_CPU_FAN, False)
    try:
        with open('debug-monitor-pid') as fp:
            txt = fp.read().split()
            mod = int(txt[0])
            pid = int(txt[1], 16)
            self.debug_monitor_pid = mod, pid
    except (FileNotFoundError, ValueError, IndexError):
        self.debug_monitor_pid = None
        wjt = self.wjt_debugpid
        if wjt.lastval != '':
            wjt.lastval = ''
            wjt.set_text('')
            wjt.bump_version()

    if self.debug_monitor_pid:
        self.sendq('O%d%04X' % self.debug_monitor_pid)

    if self.iq_index != -1:
        pass
    elif not self.vehicle_on:
        ctime = time.time()
        if ctime >= self.iq_next_start:
            start_idle_query(self)
            self.iq_next_start = math.ceil((ctime + 10) / INFO_PACKET_INTERVAL) * INFO_PACKET_INTERVAL

    ctime = time.time()
    if ctime >= self.next_info_packet:
        send_info_packet(self)

    check_delay_queue(self)
    check_time_queue(self)
    check_idle_query(self)
    check_engine_hack(self)

    if cmontime >= self.next_high_level:
        #set_charge_level(self, True)
        self.next_high_level = cmontime + 30

    if self.precondition_wait_time is not None and self.vehicle_on and not self.key_on:
        set_preconditioning(self, cmontime > self.precondition_wait_time)
    else:
        set_preconditioning(self, False)

    self.bat_voltage = volt = self.i2c_data.volts / 1000
    self.bat_current = current = self.i2c_data.current
    self.wjt_batvolt.update(volt)
    self.wjt_batcurrent.update(current)

    # If voltage drops below threshold, kill everything
    if volt > 0 and volt <= CRIT_VOLT_THRESHOLD:
        timeout = IDLE_KILL_TIMEOUT
        if self.bus_active:
            timeout = ACTIVE_KILL_TIMEOUT

        self.panic_kill_timer += 1
        if self.panic_kill_timer >= timeout:
            print('volt = %.3f, shutting down!' % volt)
            send_info_packet(self)
            time.sleep(2)
            send_info_packet(self)
            do_shutdown(self)
        else:
            print('volt = %.3f, %d/%d ticks' % (volt, self.panic_kill_timer, timeout))
    else:
        if self.panic_kill_timer:
            print('volt = %.3f, kill canceled' % volt)

        self.panic_kill_timer = 0

    self.wjt_clock.update()

def check_delay_queue(self):
    if self.delay_query_queue:
        if self.bus_active:
            cmd = self.delay_query_queue.popleft()
            if cmd:
                print('dq send: %s' % cmd)
                self.sendq(cmd)
        else:
            self.sendq('W')
    return True

def check_time_queue(self):
    if self.time_queue:
        cmd, args = self.time_queue.popleft()
        if cmd:
            cmd(*args)
    return True

def check_engine_hack(self):
    want_engine_hack = (self.vehicle_on and
                        get_config_int(self, 'enginehack', 0) and
                        self.last_cardata and
                        (self.last_cardata.air_temp1 < 95 or self.last_cardata.air_temp2 < 95) and
                        self.last_cardata.battery_raw_soc >= 35)

    if want_engine_hack:
        self.engine_hack_active = True
        self.sendq('G11')
        self.sendq('d107AE310500000000')
        #print('poke engine!')
    else:
        if self.engine_hack_active:
            self.engine_hack_active = False
            self.sendq('g11')

def do_later(self, cmd, *args):
    self.time_queue.append((cmd, args))

def start_idle_query(self):
    self.iq_data_packet_new = struct.pack('>I', int(time.time() - 1500000000))
    self.iq_data_new = []
    self.iq_index = 0
    self.iq_last_start = getmtime()
    check_idle_query(self)

def check_idle_query(self):
    cmtime = getmtime()
    if self.iq_index != -1:
        # timed out
        if cmtime >= self.iq_last_start + 20:
            print('idle query timed out!')
            # send info packet anyway
            send_info_packet(self)
            self.iq_index = -1
            return

        if self.bus_active:
            iq = self.idle_queries[self.iq_index]
            self.sendq('O%d%04X' % (iq.module, iq.pid))
        elif cmtime >= self.iq_last_wakeup + 5:
            if self.bat_voltage and self.bat_voltage < 12.1:
                print('battery voltage too low (%.2f) to send wakeup!' % self.bat_voltage)
            else:
                print('send wakeup')
                self.sendq('W')
            self.iq_last_wakeup = cmtime

def idle_query_result(self, va, vb, vc, vd):
    iq = self.idle_queries[self.iq_index]
    rawval = iq.parse_raw_val(self, va, vb, vc, vd)
    if rawval is None:
        print('failed parsing %s!' % iq.name)
        return
    txt = iq.format_val(rawval)
    print('%s = %s' % (iq.name, txt))

    self.iq_data_packet_new += iq.encode_val(rawval)
    self.iq_data_new.append(rawval)

    nextiq = self.iq_index + 1

    if nextiq >= len(IDLE_QUERIES):
        self.iq_index = -1
        self.iq_data = self.iq_data_new
        self.iq_data_packet = self.iq_data_packet_new
        send_info_packet(self)
    else:
        self.iq_index = nextiq
        check_idle_query(self)

# Periodic functions
####################################################################################

####################################################################################
# Button handling (default mode)

MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
MAJOR_SCALE.extend([j + 12 for j in MAJOR_SCALE])
MAJOR_SCALE.extend([j + 24 for j in MAJOR_SCALE])

NOTES = [int(220 * 2**(j/12)) for j in MAJOR_SCALE]

COL_FREQ = [int(300 * 2**(j/12)) for j in MAJOR_SCALE]

def phonevolume(self, up):
    if up:
        self.sendq('Mvolu')
    else:
        self.sendq('Mvold')

def carvolume(self, up):
    if up:
        if self.volume_count < 0:
            self.volume_count = 0
        else:
            self.volume_count += 1
        #sendq(self, 'R1')
    else:
        if self.volume_count > 0:
            self.volume_count = 0
        else:
            self.volume_count -= 1
        #sendq(self, 'R2')

@rotor(BUTTON_YELLOW, None, color=0xFF8888)
def seek(self, up):
    if up:
        sendq(self, 'Mtff')
    else:
        sendq(self, 'Mtrw')

@rotor(BUTTON_PURPLE, NOTES[7], show=False)
def volume(self, up):
    if get_config_int(self, 'usephonevol', 0):
        phonevolume(self, up)
    else:
        carvolume(self, up)

@rotor(BUTTON_BLUE, None, color=0x8888FF)
def fanspeed(self, up):
    if self.fanspeed_target.adjust_target(self, up):
        self.beeper.beep(NOTES[self.fanspeed_target.target], 25)
        show_overlay(self, 'FAN=%d' % self.fanspeed_target.target)
    else:
        self.beeper.beep(100, 25)


@rotor(BUTTON_GREEN, NOTES[2], color=0xFFFF88)
def track(self, up):
    if up:
        sendq(self, 'Mtfwd')
    else:
        sendq(self, 'Mtback')

@rotor(BUTTON_PINK, NOTES[0], color=0x88FF88)
def track(self, up):
    if up:
        sendq(self, 'Mtnextp')
    else:
        sendq(self, 'Mtprevp')



@rotor(BUTTON_PINK, (NOTES[0], None), mode='menu')
def temperature(self, up):
    if self.temp_target.adjust_target(self, up):
        self.beeper.beep(550, 20)
        show_overlay(self, 'TMP=%d' % self.temp_target.target)

@button(BUTTON_BLUE)
def toggle_climate(self):
    if self.climate_active:
        sendq(self, 'A9')
        show_overlay(self, '>FAN')
        self.climate_active = False
    else:
        show_overlay(self, '>ECO' if self.climate_eco else '>CFT')
        sendq(self, 'A10' if self.climate_eco else 'A11')
        self.climate_active = True

@button(BUTTON_BLUE, True)
def toggle_climate_eco(self):
    self.climate_eco = not self.climate_eco
    self.wjt_cmodesel.update(self.climate_eco)

    sendq(self, 'MeECO' if self.climate_eco else 'MeCFT')
    if self.climate_active:
        show_overlay(self, '>ECO' if self.climate_eco else '>CFT')
        sendq(self, 'A10' if self.climate_eco else 'A11')

@button(BUTTON_GREEN, True)
def dummy(self):
    pass

@button(BUTTON_PURPLE, mode='global')
def play_pause(self):
    sendq(self, 'Mtplaypause')

@button(BUTTON_PURPLE, True)
def media_interface_button(self):
    enter_media_interface(self)

@button(BUTTON_PINK, beep=None)
def marker(self):
    #sendq(self, 'MM')
    enter_text_entry(self)

@button(BUTTON_PINK, True, mode='global')
def focus_pcd(self):
    sendq(self, 'MMc')
    sendq(self, 'Mf')
    exit_button_mode(self)

@button(BUTTON_YELLOW)
def dummy(self):
    enter_menu(self)


############################################
# Menu

MENU_COLOR_BG = [
    BUTTON_CLR_PURPLE,
    BUTTON_CLR_BLUE,
    BUTTON_CLR_GREEN,
    BUTTON_CLR_PINK,
]
MENU_COLOR_FG = [0xFF000000, 0xFF000000, 0xFF000000, 0xFF000000]

def enter_menu(self):
    exit_button_mode(self)
    self.widget_config.set_visgroup(VFLAG_MENU, VFLAG_MENU)
    self.cur_button_mode = 'menu'
    num_cols = (len(self.menu_items) + 3) // 4
    middle_col = (num_cols - 1) // 2
    set_menu_pos(self, 4 * middle_col)

def update_menu_text(self):
    pos = self.menu_pos - 4
    items = self.menu_items
    for i, wjt in enumerate(self.wjt_menu):
        text = ''
        idx = i + pos
        if 0 <= idx < len(items):
            text = items[idx].text
            if callable(text):
                text = text(self)
        wjt.set_menu_text(text)

def set_menu_pos(self, pos):
    self.menu_pos = pos
    update_menu_text(self)
    self.beeper.beep(COL_FREQ[pos // 4], 20)

def menu_set_text(self, key, text):
    item = self.menu_by_key[key]
    item.text = text
    update_menu_text(self)

@rotor(BUTTON_YELLOW, beep=None, mode='menu')
def rotor_menu(self, up):
    if up:
        if self.menu_pos < len(self.menu_items) - 4:
           set_menu_pos(self, self.menu_pos + 4)
    else:
        if self.menu_pos > 3:
           set_menu_pos(self, self.menu_pos - 4)

def do_menu_select(self, ofs):
    try:
        item = self.menu_items[self.menu_pos + ofs]
    except IndexError:
        return
    if not item.func(self):
        exit_button_mode(self)


@button(BUTTON_YELLOW, False, mode='menu', beep=(300, 25, 0, 25, 200, 25))
@button(BUTTON_PINK, True, mode='menu', beep=(300, 25, 0, 25, 200, 25))
def exit_textent(self):
    exit_button_mode(self)

@button(BUTTON_PURPLE, False, mode='menu', beep=(200, 30, 300, 50))
def menu_select_black(self):
    do_menu_select(self, 0)

@button(BUTTON_BLUE, False, mode='menu', beep=(200, 30, 300, 50))
def menu_select_blue(self):
    do_menu_select(self, 1)

@button(BUTTON_GREEN, False, mode='menu', beep=(200, 30, 300, 50))
def menu_select_green(self):
    do_menu_select(self, 2)

@button(BUTTON_PINK, False, mode='menu', beep=(200, 30, 300, 50))
def menu_select_green(self):
    do_menu_select(self, 3)


def menu_checkbox(text, getval, beepon=(480, 100), beepoff=(120, 100)):
    def gettext(self):
        return ('\u2611' if getval(self) else '\u2610') + ' ' + text

    def rf(setval):
        @menu(gettext)
        def newfunc(self):
            newval = not getval(self)
            rv = setval(self, newval)
            if rv is not None:
                return rv
            beep = beepon if newval else beepoff
            if beep:
                self.beeper.beepm(beep)
            update_menu_text(self)
            return True
    return rf

def config_checkbox(text, key, defval=0):
    def rf(onchange=None):
        @menu_checkbox(text, lambda mon: get_config_int(mon, key, defval))
        def tcn(self, newval):
            set_config(self, key, str(int(newval)))
            if onchange:
                onchange(newval)
    return rf

################################################
# Column 0

config_checkbox('Brake Light Override', 'autobrk', 0)()
config_checkbox('Pause on Text Entry', 'autopause', 0)()
config_checkbox('Volume controls phone', 'usephonevol', 0)()
config_checkbox('Anti ERDTT', 'enginehack', 0)()

################################################
# Column 1

@menu('Start recording')
def start_rec(self):
    set_dc_flag('want-record', True)

@menu('Stop recording, no check')
def stop_rec(self):
    set_dc_flag('want-record', False)
    set_dc_flag('need-check', False)

@menu('Stop recording and check')
def stop_rec(self):
    set_dc_flag('want-record', False)
    set_dc_flag('need-check', True)

@menu('Stop all operations')
def stop_rec(self):
    with open('abort-all', 'w') as fp:
        pass

#@menu('Start Local Copy')
#def start_local_copy(self):
#    set_dc_flag('want-lcopy', True)

################################################
# Column 2

@menu('Windows up')
def windows_up(self):
    send_key_command(self, 'up')

@menu('Windows down')
def windows_down(self):
    send_key_command(self, 'down')

@menu('Crack windows')
def crack_windows(self):
    send_key_command(self, 'crack')

@menu('Open hatch')
def open_hatch(self):
    send_key_command(self, 'hatch')

################################################
# Column 3

@menu('Max defrost')
def toggle_defrost(self):
    do_later(self, lambda: self.sendq('A29'))
    do_later(self, lambda: None)
    if self.cardata.vent == 2:
        do_later(self, lambda: self.fanspeed_target.set_target(self, self.old_fanspeed))
    else:
        self.old_fanspeed = self.fanspeed_target.get_current_val(self)
        do_later(self, lambda: self.fanspeed_target.set_target(self, 8))


@menu_checkbox('Recirc', lambda mon: mon.climate_recirc)
def tcn(self, newval):
    self.climate_recirc = newval
    check_recirc_mode(self)

@menu_checkbox('Mount cameras', lambda mon: exists('want-mount'))
def set_mount(self, newval):
    fpath = 'want-mount'
    if newval:
        with open(fpath, 'w') as fp:
            pass
    else:
        os.unlink(fpath)


@menu_checkbox('HUD dump', lambda mon: mon.widget_config.hdr.visibility & VFLAG_DUMP_PNG)
def hud_dump(self, newval):
    if newval:
        self.widget_config.set_visgroup(VFLAG_DUMP_PNG, VFLAG_DUMP_PNG)
    else:
        self.widget_config.set_visgroup(VFLAG_DUMP_PNG, 0)

################################################
# Column 4


@menu('Panic off')
def panic_off(self):
    send_key_command(self, 'panicoff')

@menu('Diag release lights')
def diag_release(self):
    clear_diag_lights(self)

config_checkbox('CAN logging', 'enable_canlog', 0)()

@menu('Shutdown')
def reboot(self):
    ctime = getmtime()
    if ctime < self.reboot_confirm_time:
        do_shutdown(self)
    else:
        self.reboot_confirm_time = ctime + 0.5
        return True

# Menu
############################################

############################################
# Text Entry

def enter_text_entry(self):
    exit_button_mode(self)
    self.widget_config.set_visgroup(VFLAG_TEXT_ENTRY, VFLAG_TEXT_ENTRY)

    if get_config_int(self, 'autopause', 0) and self.music_data[-1]:
        self.auto_resume = True
        self.sendq('Mtpause')

    self.cur_button_mode = 'textent'
    self.wjt_textent.cfg = 0xFFFFFF
    if not self.cur_text:
        sendq(self, 'MMt')
        if self.logger is not None:
            self.logger.log_marker('')

    set_textent_column(self, 4)
    update_text_widget(self)

def set_textent_column(self, col):
    for wjt in self.textent_widgets[self.textent_column]:
        wjt.cbg = wjt.nsel_bg
        wjt.cfg = wjt.nsel_fg
        wjt.bump_version()
    if col == 4:
        self.beeper.beepm((COL_FREQ[col], 30, COL_FREQ[col] - 60, 30, COL_FREQ[col], 30))
    else:
        self.beeper.beep(COL_FREQ[col], 70 if col == 4 else 20)
    bigw = self.textent_widgets[10]
    otxt = ''
    self.textent_column = col
    for i, wjt in enumerate(self.textent_widgets[col]):
        bigw[i].set_text(wjt.key)
        bigw[i].cbg = wjt.sel_bg
        bigw[i].cfg = wjt.sel_fg
        bigw[i].bump_version()

        wjt.cbg = wjt.sel_bg
        wjt.cfg = wjt.sel_fg
        wjt.bump_version()

@rotor(BUTTON_PURPLE, beep=None, mode='textent')
@rotor(BUTTON_GREEN, beep=None, mode='textent')
@rotor(BUTTON_BLUE, beep=None, mode='textent')
@rotor(BUTTON_PINK, beep=None, mode='textent')
def rotate_textent_column(self, up):
    ncol = self.textent_column + (1 if up else -1)
    if ncol < 0 or ncol > 9:
        return
    set_textent_column(self, ncol)

@rotor(BUTTON_YELLOW, beep=None, mode='textent')
def rotate_textent_cursor(self, up):
    if up:
        if self.text_pos < len(self.cur_text):
            self.beeper.beep(440, 20)
            self.text_pos += 1
            update_text_widget(self)
    else:
        if self.text_pos > 0:
            self.beeper.beep(380, 20)
            self.text_pos -= 1
            update_text_widget(self)

def update_text_widget(self):
    self.wjt_textent.set_text(self.cur_text)
    self.wjt_textentcurs.set_text(' ' * self.text_pos + '_')
    self.wjt_textent.bump_version()
    self.wjt_textentcurs.bump_version()

def insert_text(self, text):
    self.cur_text = self.cur_text[:self.text_pos] + text + self.cur_text[self.text_pos:]
    self.text_pos += len(text)
    update_text_widget(self)

def enter_text(self, row):
    wjt = self.textent_widgets[self.textent_column][row]
    insert_text(self, wjt.key)


@button(BUTTON_YELLOW, mode='textent', beep=(600, 30))
def enter_row0(self):
    enter_text(self, 0)

@button(BUTTON_PURPLE, mode='textent', beep=(600, 30))
def enter_row1(self):
    enter_text(self, 1)

@button(BUTTON_BLUE, mode='textent', beep=(600, 30))
def enter_row2(self):
    enter_text(self, 2)

@button(BUTTON_GREEN, mode='textent', beep=(600, 30))
def enter_row3(self):
    enter_text(self, 3)

@button(BUTTON_PINK, False, mode='textent', beep=(300, 25, 0, 25, 200, 25))
def exit_textent(self):
    exit_button_mode(self)


@button(BUTTON_YELLOW, True, mode='textent', beep=(300, 25, 250, 25, 200, 40))
def cancel_text(self):
    self.cur_text = ''
    self.text_pos = 0
    exit_button_mode(self)

@button(BUTTON_PURPLE, True, mode='textent', beep=None)
def backspace(self):
    if self.text_pos > 0:
        self.beeper.beep(120, 30)
        self.cur_text = self.cur_text[:self.text_pos - 1] + self.cur_text[self.text_pos:]
        self.text_pos -= 1
        update_text_widget(self)

@button(BUTTON_GREEN, True, mode='textent')
def enter_extra_marker(self, beep=(900, 20)):
    sendq(self, 'MMt~')
    if self.logger is not None:
        self.logger.log_marker('~')
    update_text_widget(self)

@button(BUTTON_BLUE, True, mode='textent', beep=(700, 30))
def enter_space(self):
    insert_text(self, ' ')

@button(BUTTON_PINK, True, mode='textent', beep=(200, 30, 300, 50))
def accept_text(self):
    sendq(self, 'MMt' + self.cur_text)
    if self.logger is not None:
        self.logger.log_marker(self.cur_text)
    self.cur_text = ''
    self.text_pos = 0
    exit_button_mode(self)

# Text Entry
####################################################################################

####################################################################################
# Media Interface

def enter_media_interface(self):
    exit_button_mode(self)
    self.widget_config.set_visgroup(VFLAG_MEDIA_INTERFACE, VFLAG_MEDIA_INTERFACE)

    self.cur_button_mode = 'media'
    self.sendq('MIu')
    update_media_interface(self)

def update_media_interface(self):
    if not self.media_interface_data:
        return

    hdr, line1, line2, btn1, btn2, btn3 = self.media_interface_data[:6]

    self.wjt_media_intf_header.check_set_text(hdr)
    self.wjt_media_intf_line1.check_set_text(line1)
    self.wjt_media_intf_line2.check_set_text(line2)
    self.wjt_media_intf_btn1.check_set_text(btn1)
    self.wjt_media_intf_btn2.check_set_text(btn2)
    self.wjt_media_intf_btn3.check_set_text(btn3)

@rotor(BUTTON_YELLOW, NOTES[4], mode='media')
def media_rotor(self, up):
    if up:
        self.sendq('Mtright')
    else:
        self.sendq('Mtleft')

@button(BUTTON_PURPLE, True, mode='media', beep=(300, 25, 0, 25, 200, 25))
def exit_media_interface(self):
    exit_button_mode(self)

@button(BUTTON_YELLOW, False, mode='media')
def media_select(self):
    self.sendq('Mtselect')

@button(BUTTON_BLUE, False, mode='media')
def media_button1(self):
    self.sendq('Mtb1')

@button(BUTTON_GREEN, False, mode='media')
def media_button1(self):
    self.sendq('Mtb2')

@button(BUTTON_PINK, False, mode='media')
def media_button3(self):
    self.sendq('Mtb3')



# Media Interface
####################################################################################

def set_charge_level(self, hi):
    txt = 'S2108640807%s000000000000' % ('10' if hi else '20')
    sendq(self, txt)

#@button(BUTTON_PINK)
def l1_charge_level(self):
    lvlhi = getattr(self, 'charge_level_hi', False)
    lvlhi = self.charge_level_hi = not lvlhi
    txt = 'S2108640807%s000000000000' % ('10' if lvlhi else '20')
    sendq(self, txt)

@button(BUTTON_YELLOW, True, beep=None)
def force_connect(self):
    self.force_connect = not self.force_connect
    self.beeper.beep(480 if self.force_connect else 120, 100)
    print('force connect = %d' % self.force_connect)
    update_active(self, getmtime())

####################################################################################
# Button / rotor handling

def exit_button_mode(self):
    self.cur_button_mode = 'default'

    self.widget_config.set_visgroup(VFLAG_TEXT_ENTRY | VFLAG_MENU | VFLAG_MUSIC | VFLAG_MEDIA_INTERFACE, 0)

    if get_config_int(self, 'autopause', 0) and self.auto_resume:
        self.sendq('Mtplay')

    self.auto_resume = False

    clear_overlay(self)
    self.overlay_expire = 0

    self.wjt_textent.cfg = 0x777777
    update_text_widget(self)


def get_button_func(self, btn, type):
    f = button_mode[self.cur_button_mode][type].get(btn)
    if not f:
        f = button_mode['global'][type].get(btn)
    return f


def dispatch_rotor_event(self, btn, up):
    f = get_button_func(self, btn, 2)
    if f:
        f[0](self, up)

def dispatch_button_event(self, btn, longp):
    f = get_button_func(self, btn, int(bool(longp)))
    if f:
        f[0](self)

def gpio_event(self, evt):
    btn = evt & 0x1F
    rotor = bool(evt & 0x80)
    release = bool(evt & 0x40)
    longp = bool(evt & 0x20)

    if rotor:
        self.last_rotor = getmtime()
        f = get_button_func(self, btn, 2)
        if f:
            beepf = f[1]
            if isinstance(beepf, tuple):
                beepf = beepf[1]
            if beepf:
                self.beeper.beep(beepf, 25)
            f[0](self, release)
    else:
        self.cur_button_press = (0, False) if release else (btn, longp)

        event = None
        if release:
            if not longp:
                event = 0
        else:
            if longp:
                event = 1

        if event is not None:
            f = get_button_func(self, btn, event)
            if f:
                if f[1]:
                    self.beeper.beepm(f[1])
                f[0](self)

# Button / rotor handling
####################################################################################

####################################################################################
# Odometer interpolation

class OdoRecalc:
    def __init__(self):
        self.base_lo = 0
        self.base_hi = 0
        self.wrc_lo = 0
        self.wrc_hi = 0
        self.offset = 0.0 #0.51
        self.last_raw_odo = 0

        self.last_odometer = None
        self.trip_distance = 0
        self.ev_distance = 0


        self.upm = 31956

    def recalc(self, cd):
        if cd.wrc3 >= self.wrc_hi:
            new_odometer = self.base_hi + (cd.wrc3 - self.wrc_hi) / self.upm
        elif cd.wrc3 >= self.wrc_lo:
            new_odometer = (cd.wrc3 - self.wrc_lo) * (self.base_hi - self.base_lo) / (self.wrc_hi - self.wrc_lo) + self.base_lo
        else:
            new_odometer = cd.raw_odometer / DISTANCE_CONVERSION

        if cd.raw_odometer != self.last_raw_odo:
            self.last_raw_odo = cd.raw_odometer
            src_conv = cd.raw_odometer / DISTANCE_CONVERSION
            if abs(src_conv - new_odometer) > 0.1:
                self.wrc_lo = cd.wrc3
                self.base_lo = src_conv
                self.wrc_hi = cd.wrc3
                self.base_hi = src_conv
                new_odometer = src_conv
            else:
                self.base_lo = new_odometer
                self.wrc_lo = cd.wrc3
                self.base_hi = src_conv + 0.1
                self.wrc_hi = cd.wrc3 + (self.upm * 0.1)

        new_odometer += self.offset
        cd.odometer_km = new_odometer * 1.609344
        cd.odometer = distance_to_db(new_odometer)

        if self.last_odometer is None:
            self.last_odometer = cd.odometer

        delta = cd.odometer - self.last_odometer
        self.last_odometer = cd.odometer

        self.trip_distance += delta
        if not cd.rpm:
            self.ev_distance += delta
        cd.trip_distance = self.trip_distance
        cd.trip_ev_distance = self.ev_distance

# Odometer interpolation
####################################################################################

####################################################################################
# Diag light control

def update_diag_register(self, reg):
    regval = 0
    for name, (nreg, offmask, onmask) in DIAG_REGISTERS.items():
        if reg == nreg:
            val = self.diag_lights.get(name)
            if val is True:
                regval |= onmask
            elif val is False:
                regval |= offmask
    ctime = getmtime()
    cmd = 'd007AE%02X%010X' % (reg, regval)
    self.sendq(cmd)
    if not self.vehicle_on and (ctime - self.last_diag_light_send) > 1.0:
        # After a period of inactivity with the vehicle off, sometimes the first command
        # gets lost. Double up the command in case the first one is missed.
        self.sendq(cmd)
    self.last_diag_light_send = ctime


def set_diag_lights(self, newvals):
    update = {}
    for name, val in newvals.items():
        combo = DIAG_COMBOS.get(name)
        if combo:
            for name in combo:
                update[name] = val
        else:
            update[name] = val

    regs = set()
    had_vals = bool(self.diag_lights)

    for name, val in update.items():
        info = DIAG_REGISTERS.get(name)
        dct = self.diag_lights
        if info:
            regs.add(info[0])
            dct = self.diag_lights
            if val is None:
                dct.pop(name, None)
            else:
                dct[name] = val

    have_vals = bool(self.diag_lights)

    if had_vals != have_vals:
        if have_vals:
            self.sendq('G002')
        else:
            self.sendq('g002')

    for reg in regs:
        update_diag_register(self, reg)

def clear_diag_lights(self):
    self.sendq('d002AE')
    if self.diag_lights:
        self.sendq('g002')
    self.diag_lights.clear()

# Diag light control
####################################################################################

####################################################################################
# CarData frame handling

def update_cd(bs, field, bits, signed, cd, lcd, full):
    if full or bs.read_bits(1):
        if signed:
            val = bs.read_bits_signed(bits)
        else:
            val = bs.read_bits(bits)
        setattr(cd, field, val)
    else:
        setattr(cd, field, getattr(lcd, field))

def parse_cardata(bs, cd, lcd, expect_seq):
    full = bool(bs.read_bits(1))
    if full:
        expect_seq = 0
    else:
        seq = bs.read_bits(4)
        if seq != expect_seq:
            return -1
        expect_seq = (seq + 1) & 15

    #AUTO START : custom_monitor handle_data_frame
    update_cd(bs, 'wrc3', 25, False, cd, lcd, full)
    update_cd(bs, 'wrc1', 13, False, cd, lcd, full)
    update_cd(bs, 'wrc2', 13, False, cd, lcd, full)
    update_cd(bs, 'mga_rpm', 15, True, cd, lcd, full)
    update_cd(bs, 'mgb_rpm', 15, True, cd, lcd, full)
    update_cd(bs, 'rawspeed', 14, False, cd, lcd, full)
    update_cd(bs, 'hv_amps', 16, True, cd, lcd, full)
    update_cd(bs, 'mga_amps', 16, True, cd, lcd, full)
    update_cd(bs, 'mgb_amps', 16, True, cd, lcd, full)
    update_cd(bs, 'hv_volts', 16, False, cd, lcd, full)
    update_cd(bs, 'mga_volts', 16, False, cd, lcd, full)
    update_cd(bs, 'mgb_volts', 16, False, cd, lcd, full)
    update_cd(bs, 'steer', 16, True, cd, lcd, full)
    update_cd(bs, 'brake_pct', 8, False, cd, lcd, full)
    update_cd(bs, 'accel_pct', 8, False, cd, lcd, full)
    update_cd(bs, 'rpm', 14, False, cd, lcd, full)
    update_cd(bs, 'fuel_ctr', 21, False, cd, lcd, full)
    update_cd(bs, 'climate_power', 7, False, cd, lcd, full)
    update_cd(bs, 'climate_mode', 2, False, cd, lcd, full)
    update_cd(bs, 'heat_ac', 2, False, cd, lcd, full)
    update_cd(bs, 'battery_raw_soc', 8, False, cd, lcd, full)
    update_cd(bs, 'battery_soc', 8, False, cd, lcd, full)
    update_cd(bs, 'raw_odometer', 25, False, cd, lcd, full)
    update_cd(bs, 'range', 16, False, cd, lcd, full)
    update_cd(bs, 'scflags', 24, False, cd, lcd, full)
    update_cd(bs, 'clutch_state', 8, False, cd, lcd, full)
    update_cd(bs, 'rawccspeed', 13, False, cd, lcd, full)
    update_cd(bs, 'ccbtn', 4, False, cd, lcd, full)
    update_cd(bs, 'radiobtn', 4, False, cd, lcd, full)
    update_cd(bs, 'coolant_temp', 8, False, cd, lcd, full)
    update_cd(bs, 'intake_temp', 8, False, cd, lcd, full)
    update_cd(bs, 'battery_temp', 8, False, cd, lcd, full)
    update_cd(bs, 'lat', 31, True, cd, lcd, full)
    update_cd(bs, 'lon', 31, True, cd, lcd, full)
    update_cd(bs, 'air_temp1', 8, False, cd, lcd, full)
    update_cd(bs, 'air_temp2', 8, False, cd, lcd, full)
    update_cd(bs, 'air_pressure', 8, False, cd, lcd, full)
    update_cd(bs, 'tire_ft_lf', 8, False, cd, lcd, full)
    update_cd(bs, 'tire_rr_lf', 8, False, cd, lcd, full)
    update_cd(bs, 'tire_ft_rt', 8, False, cd, lcd, full)
    update_cd(bs, 'tire_rr_rt', 8, False, cd, lcd, full)
    update_cd(bs, 'oil_life', 8, False, cd, lcd, full)
    update_cd(bs, 'fanspeed', 8, False, cd, lcd, full)
    update_cd(bs, 'vent', 3, False, cd, lcd, full)
    update_cd(bs, 'select_fanspeed', 5, False, cd, lcd, full)
    update_cd(bs, 'select_temp', 6, False, cd, lcd, full)
    update_cd(bs, 'recirc', 2, False, cd, lcd, full)
    update_cd(bs, 'gear', 3, False, cd, lcd, full)
    update_cd(bs, 'drive_mode', 2, False, cd, lcd, full)
    update_cd(bs, 'rear_defrost', 1, False, cd, lcd, full)
    #AUTO END

    return expect_seq

ignore = set(['air_pressure', 'air_temp1', 'coolant_temp', 'mgb_volts', 'mga_volts', 'hv_volts', 'vent'])
def handle_data_frame(self, fw_millis, bs):
    #print(int(getmtime() * 1000) - bs.stxtime)
    cd = self.cardata
    lcd = self.last_cardata

    cfw = self.cur_fw_millis
    if fw_millis < (cfw & 0x3FFFFFFF):
        cfw += 0x40000000

    self.cur_fw_millis = cfw = (cfw & ~0x3FFFFFFF) | fw_millis
    cd.fw_millis = cfw

    check_overlay(self)

    st = getmtime()


    #self.expect_seq = seq = parse_cardata(bs, cd, lcd, self.expect_seq)
    self.expect_seq = seq = bs.parse_cardata(cd, lcd, self.expect_seq)

    if seq == -1:
        sendq(self, 'F')
        print('out of sequence!')
        return

    et = getmtime()

    if cd.gear == 0 and cd.clutch_state != 0:
        self.gear_mismatch_count += 1
        if self.gear_mismatch_count >= 30:
            if et >= self.last_swcan_warn + 10:
                self.last_swcan_warn = et
                self.beeper.beepm([COL_FREQ[5], 150, COL_FREQ[3], 150, COL_FREQ[1], 150])
    else:
        self.gear_mismatch_count = 0

    update_motion_state(self, (fw_millis - lcd.fw_millis) & 0x3FFFFFFF, cd.gear, cd.rawspeed)

    cd.motion_state = self.motion_state

    if self.logger:
        self.logger.log_data_frame(bs.stxtime, fw_millis, cd, lcd)

    fsdelta = (cd.select_fanspeed & 0xF) - (lcd.select_fanspeed & 0xF)
    tempdelta = TemperatureTarget.convert(cd.select_temp) - TemperatureTarget.convert(lcd.select_temp)

    ctypes.memmove(ctypes.addressof(lcd), ctypes.addressof(cd), ctypes.sizeof(lcd))

    if cd.lat and cd.lon:
        self.last_lat = cd.lat
        self.last_lon = cd.lon

    if fsdelta != 0:
        self.fanspeed_target.move_to_target(self, fsdelta)

    if tempdelta != 0:
        self.temp_target.move_to_target(self, tempdelta)

    want_fan_double = cd.climate_mode == 1
    if want_fan_double != self.climate_fan_double:
        self.climate_fan_double = want_fan_double
        current_speed = self.fanspeed_target.get_current_val(self)

        if current_speed != 0:
            if want_fan_double:
                new_speed = current_speed * 2
            else:
                new_speed = (current_speed + 1) // 2
            new_speed = max(1, min(new_speed, 8))

            if new_speed != current_speed:
                self.fanspeed_target.set_target(self, new_speed)

    self.odo.recalc(cd)
    new_range = int(cd.range / DISTANCE_CONVERSION + 0.1)
    if new_range != self.last_range:
        full_range = (cd.odometer - self.last_full_odo) + new_range * 1000
        self.range_samples.appendleft((cd.odometer, new_range, full_range))
        while len(self.range_samples) > 5:
            self.range_samples.pop()

        self.last_range = new_range
        self.last_range_odo = cd.odometer

    self.climate_active = cd.climate_mode != 2

    if cd.battery_soc >= 252:
        force_full_charge(self)

    new_brake_state = BRAKE_OFF
    if get_config_int(self, 'autobrk', 0):
        if cd.gear == 4 and cd.accel_pct < 15 and not bool(cd.rawccspeed & 0x1000):
            new_brake_state = BRAKE_ON
            if cd.scflags & FLAG1_LEFT_TURN:
                new_brake_state = BRAKE_ON_LSIG

            if cd.scflags & FLAG1_RIGHT_TURN:
                new_brake_state = BRAKE_ON_RSIG

    if new_brake_state != self.brake_light_state:
        self.brake_light_state = new_brake_state
        update = {
            'cbrk': (True if new_brake_state != BRAKE_OFF else None),
            'lrsig': (True if new_brake_state != BRAKE_OFF and new_brake_state != BRAKE_ON_LSIG else None),
            'rrsig': (True if new_brake_state != BRAKE_OFF and new_brake_state != BRAKE_ON_RSIG else None)
        }
        set_diag_lights(self, update)

    with open('/dev/shm/wjt_text', 'w') as fp:
        for w in self.cardata_widgets:
            w.check(cd, self)
            fp.write('%-30s = %s\n' % (type(w).__name__, w.textbuf.value.decode('utf8')))

# CarData frame handling
####################################################################################

####################################################################################
# Config vars

def save_config(self):
    fn = 'monitor_config.json'
    with open(join(dirname(__file__), fn + '~'), 'w') as fp:
        json.dump(self.config, fp, indent=True)
    os.rename(fn + '~', fn)

def set_config(self, key, val):
    if val:
        self.config[key] = val
    else:
        self.config.pop(key, None)

    save_config(self)

def get_config(self, key, defaultval=None):
    return self.config.get(key, defaultval)

def get_config_int(self, key, defaultval=None):
    try:
        return int(self.config.get(key, defaultval))
    except (ValueError, TypeError):
        return defaultval

# Config vars
####################################################################################

####################################################################################
# Message handling

@msg('C')
def force_full_charge(self, msgtype=None, msgtxt=None):
    cd = self.cardata
    lfo = cd.odometer
    if self.vehicle_on:
        lfo -= cd.trip_distance

    if lfo != self.last_full_odo:
        self.last_full_odo = lfo
        with open(LAST_FULL_ODO_PATH, 'w') as fp:
            fp.write('%d\n' % lfo)

@msg('c')
def set_config_msg(self, msgtype, msgtxt):
    key, sep, val = msgtxt.partition('=')
    if not sep:
        return
    set_config(self, key, val)

@msg('W')
def windowsize(self, msgtype, msgtxt):
    w, h = map(int, msgtxt.split(','))
    #self.log('set window size %s %r' % (chr(msgtype), msgtxt))
    tmpbuf = array.array('H', (h, w))
    fcntl.ioctl(self.shell_fd, termios.TIOCSWINSZ, tmpbuf)

_buttons = {
    'y': BUTTON_YELLOW,
    'p': BUTTON_PURPLE,
    'b': BUTTON_BLUE,
    'g': BUTTON_GREEN,
    'r': BUTTON_PINK
}

@msg('u')
def sim_input(self, msgtype, msgtxt):
    which = _buttons.get(msgtxt[:1])
    typ =  msgtxt[1:2]
    if btn is None:
        return

    if typ == 'u':
        dispatch_rotor_event(self, btn, True)
    elif typ == 'd':
        dispatch_rotor_event(self, btn, False)
    elif typ == 'l':
        dispatch_button_event(self, btn, True)
    else:
        dispatch_button_event(self, btn, False)

def update_music(self):
    try:
        artist, album, title, track, year, playing = self.music_data[:7]
    except Exception:
        return

    self.wjt_music_title.playing = playing
    self.wjt_music_title.curtitle = title
    artist_text = '%s | %s' % (artist, album)
    try:
        track = '%02d' % int(track)
    except ValueError:
        pass

    if track:

        artist_text += ' / ' + track

    self.wjt_music_artist.set_text(artist_text)
    self.wjt_music_title.update()
    self.wjt_music_title.bump_version()
    self.wjt_music_artist.bump_version()


@msg('U')
def music_message(self, msgtype, msgtxt):
    try:
        was_playing = self.music_data[-1]
        self.music_data = json.loads(msgtxt)
        if not was_playing and self.music_data[-1]:
            self.auto_resume = False

        update_music(self)
    except (TypeError, ValueError, IndexError):
        pass

@msg('D')
def music_position(self, msgtype, msgtxt):
    try:
        position, duration, volume = json.loads(msgtxt)
        self.wjt_music_time.curpos = position
        self.wjt_music_time.curdur = duration
        if self.wjt_music_time.volume != volume:
            show_overlay(self, 'VOL=%d' % volume)
            self.wjt_music_time.volume = volume

        self.wjt_music_time.update()
        self.wjt_music_time.bump_version()
    except (TypeError, ValueError, IndexError):
        pass

@msg('I')
def media_interface(self, msgtype, msgtxt):
    try:
        self.media_interface_data = json.loads(msgtxt)
        update_media_interface(self)
    except (TypeError, ValueError, IndexError):
        pass

@msg('B')
def beep_message(self, msgtype, msgtxt):
    print('beep')
    logtxt = '|%r' % (time.time() + 0.05)
    self.sendq('MMt' + logtxt)
    if self.logger is not None:
        self.logger.log_marker(logtxt)
    bd = 150
    self.beeper.beepm([0, 50, 2000, bd, 1670, bd, 2000, bd, 2670, bd, 0, bd])

@msg('F')
def force_connect_on(self, msgtype, msgtxt):
    self.force_connect = True
    update_active(self, getmtime())

@msg('f')
def force_connect_on(self, msgtype, msgtxt):
    self.force_connect = False
    update_active(self, getmtime())

@msg('k')
def key_command_msg(self, msgtype, msgtxt):
    send_key_command(self, msgtxt)

@msg('q')
def trigger_idle_query(self, *a):
    self.iq_next_start = 0
    print('query triggered')

@msg('h')
def msg_diag(self, msgtype, msgtext):
    val = None
    if msgtext.startswith('+'):
        val = True
    elif msgtext.startswith('-'):
        val = False
    elif msgtext.startswith('/'):
        val = None
    else:
        print('invalid diag command: %r' % msgtext)
    names = [v for v in msgtext[1:].split(',') if v]
    if val is None and not names:
        clear_diag_lights(self)
        return

    set_diag_lights(self, {name: val for name in names})

@msg('d')
def msg_dccmd(self, msgtype, msgtext):
    if msgtext == 'start':
        set_dc_flag('want-record', True)

    elif msgtext == 'stop':
        set_dc_flag('want-record', False)

    elif msgtext == 'stopcheck':
        set_dc_flag('want-record', False)
        set_dc_flag('need-check', True)

    elif msgtext == 'stopnocheck':
        set_dc_flag('want-record', False)
        set_dc_flag('need-check', False)

    elif msgtext == 'check':
        set_dc_flag('need-check', True)

    elif msgtext == 'nocheck':
        set_dc_flag('need-check', False)

    elif msgtext == 'mount':
        set_dc_flag('want-mount', True)

    elif msgtext == 'umount':
        set_dc_flag('want-mount', False)

    elif msgtext == 'resethub':
        set_dc_flag('reset-hub', True)

    elif msgtext == 'abort':
        set_dc_flag('abort-all', True)

@msg('H')
def set_hud_dump(self, msgtype, msgtext):
    if msgtext == '1':
        self.widget_config.set_visgroup(VFLAG_DUMP_PNG, VFLAG_DUMP_PNG)
    elif msgtext == '0':
        self.widget_config.set_visgroup(VFLAG_DUMP_PNG, 0)

@msg('L')
def enable_canlog(self, msgtype, msgtext):
    print('enable canlog')
    set_config(self, 'enable_canlog', '1')
    if msgtext == 'f':
        self.force_canlog = True

@msg('l')
def disable_canlog(self, msgtype, msgtext):
    print('disable canlog')
    set_config(self, 'enable_canlog', '0')
    self.force_canlog = False

@msg('i')
def send_info_packet(self, *a):
    srcaddr = get_iface_address('ppp0')
    if srcaddr is None:# or not self.iq_data_packet:
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((srcaddr, 0))

    cd = self.cardata
    flags = 0

    if self.vehicle_on:
        flags |= FLAG_POWER_ON

    if self.key_on:
        flags |= FLAG_KEY_ON

    if self.preconditioning:
        flags |= FLAG_PRECONDITIONING

    if self.lock:
        flags |= FLAG_LOCK

    packet = self.iq_data_packet
    if len(packet) < self.idle_query_len:
        packet += b'\0' * (self.idle_query_len - len(packet))
    extra_data = struct.pack('>BBBBBBBBIiiH', cd.air_temp1, min(255, max(0, (self.current_temp + 40))), cd.oil_life, cd.tire_ft_lf,
                             cd.tire_rr_lf, cd.tire_ft_rt, cd.tire_rr_rt, flags, cd.odometer, cd.lat, cd.lon, int(self.i2c_data.volts // 10))

    pkt = self.info_hmac.build_message(packet + extra_data)

    sock.sendto(pkt, (CONFIG['info_server'], CONFIG['info_port']))
    sock.close()
    self.next_info_packet = math.ceil((time.time() + 10) / 1800) * 1800 + 10

@msg('G')
def msg_gps(self, msgtype, msgtext):
    try:
        data = json.loads(msgtext)
        self.last_lat = int(data[0] * 3600000)
        self.last_lon = int(data[1] * 3600000)

        if self.logger:
            self.logger.log_gps(data)

    except (ValueError, TypeError, IndexError):
        return

def parse_message(self, msgtype, msgtxt):
    '''Called by serial_monitor when a message is received from phone, or from parse_frame when FT_PTMSG is received'''
    #self.log('msg: %s %r' % (msgtype, msgtxt))
    handler = message_handlers.get(msgtype)
    if handler:
        handler(self, msgtype, msgtxt)

# Message handling
####################################################################################

####################################################################################
# Frame handling

def log_dtc(self, responding_mod, dtc):
    if dtc:
        key = responding_mod, dtc
        if key not in self.trip_dtc_seen:
            self.trip_dtc_seen.add(key)
            code = '%s%04X' % ("PCBU"[(dtc >> 14) & 3], dtc & 0x3FFF)
            print('DTC: %d %s' % (responding_mod, code))

def parse_frame(self, bs):
    '''Called by serial_monitor when a frame is received from Macchina'''
    fw_millis = bs.read_bits(30)
    ftype = bs.read_bits(3)

    if ftype == FT_DATA:
        handle_data_frame(self, fw_millis, bs)

    elif ftype == FT_REPLY:
        rtype = bs.read_bits(7)
        handler = reply_handlers.get(chr(rtype))
        if handler:
            handler(self, fw_millis, bs)

    elif ftype == FT_OBD:
        mod = bs.read_bits(3)
        pid = bs.read_bits(16)
        vb = bs.read_bits(8)
        va = bs.read_bits(8)
        vc = bs.read_bits(8)
        vd = bs.read_bits(8)
        if (mod, pid) == self.debug_monitor_pid:
            txt = '%02X %02X %02X %02X' % (va, vb, vc, vd)
            wjt = self.wjt_debugpid
            if txt != wjt.lastval:
                wjt.lastval = txt
                wjt.set_text(txt)
                wjt.bump_version()

        iq = None
        if self.iq_index != -1:
            iq = self.idle_queries[self.iq_index]
            if pid == iq.pid:
                idle_query_result(self, va, vb, vc, vd)
            else:
                iq = None

        if iq is None:
            print('OBD reply: %d %04x = %02x %02x %02x %02x' % (mod, pid, va, vb, vc, vd))

        outf = 'obd-%04X-%d.txt' % (pid, mod)
        with open(outf + '~', 'w') as fp:
            fp.write('%02x %02x %02x %02x\n' % (va, vb, vc, vd))
        os.rename(outf + '~', outf)

    elif ftype == FT_EVENT:
        evt = bs.read_bits(6)
        try:
            ename = EVENT_NAMES[evt]
        except IndexError:
            ename = 'EVENT_%d' % evt
        self.log('event: %s' % ename)
        handler = event_handlers.get(evt)
        if handler:
            handler(self, fw_millis, bs)
        if self.logger:
            self.logger.log_event(bs.stxtime, fw_millis, evt)

    elif ftype == FT_TCODE:
        responding_mod = bs.read_bits(3)
        total_count = bs.read_bits(8)
        dtc1 = bs.read_bits(16)
        dtc2 = bs.read_bits(16)

        log_dtc(self, responding_mod, dtc1)
        log_dtc(self, responding_mod, dtc2)

    elif ftype == FT_PTMSG:
        msgtype = chr(bs.read_bits(7))
        msgtxt = bytearray()
        try:
            while True:
                b = bs.read_bits(7)
                if b == 0:
                    break
                msgtxt.append(b)
        except IndexError:
            pass

        msgtxt = msgtxt.decode('ascii')
        parse_message(self, msgtype, msgtxt)

# Frame handling
####################################################################################

#######################################################################
#
# HUD
#
#######################################################################

all_widgets = []

wjt = widget_decorator(all_widgets)

class BaseWidget(Widget):
    last_rawval = None
    field = None
    fmt = '%d'
    flags = 0
    bg = 0
    update_from_data = False

    visgroup = VFLAG_VEHICLE_ON | VFLAG_DISPLAY_ON
    vismask = VFLAG_VEHICLE_ON | VFLAG_DISPLAY_ON

    def get_rawval(self, cd, mon):
        if self.field is None:
            return None
        return getattr(cd, self.field)

    def update_rawval(self, rv):
        if rv is None:
            self.set_text('')
        else:
            self.set_text(self.fmt % rv)

    def check(self, cd, mon):
        rv = self.get_rawval(cd, mon)
        if rv != self.last_rawval:
            self.last_rawval = rv
            self.update_rawval(rv)
            self.bump_version()


@wjt
class SpeedWidget(BaseWidget):
    w = 100
    h = 100
    xo = 1
    yo = 88
    textsize = 100
    fg = 0x88FF88
    xscale = .7
    nchar = 2
    update_from_data = True

    fmt = '%02d'

    xpos = CENTER_OF, 'screen', -140
    ypos = AT_TOP, 'screen', 10

    def get_rawval(self, cd, mon):
        return int(cd.rawspeed / DISTANCE_CONVERSION + 0.5), mon.brake_light_state != BRAKE_OFF

    def update_rawval(self, rv):
        self.cfg = (0xFFFF88 if rv[1] else 0x88FF88)
        self.set_text('%02d' % rv[0])



@wjt
class CCSpeedWidget(BaseWidget):
    w = 80
    h = 80
    xo = 6
    yo = 68
    textsize = 72
    fg = 0x808080
    xscale = .7
    nchar = 2

    field = 'rawccspeed'
    update_from_data = True

    xpos = ON_RIGHT, 'SpeedWidget'
    ypos = AT_TOP, 'SpeedWidget'

    ccactive = False

    def get_rawval(self, cd, mon):
        rv = cd.rawccspeed
        return bool(rv & 0x1000), int((rv & 0xFFF) * 4 / DISTANCE_CONVERSION + 0.5)

    def update_rawval(self, rv):
        ccactive, speed = rv
        self.cfg = 0xFFFFFF if ccactive else 0xFFbbbb
        self.cstrike = 0x01FFBBBB if not ccactive else 0
        self.set_text('%02d' % speed)

class RangeWidgetBase(BaseWidget):
    w = 60
    h = 46
    xo = 6
    yo = 41
    textsize = 48
    fg = 0xFFFFFF
    #bg = 0xFF888888
    xscale = .7
    nchar = 5

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

@wjt
class DistSinceFullWidget(RangeWidgetBase):
    update_from_data = True
    textsize = 32
    yo = 28
    h = 32

    xpos = ON_RIGHT, 'CCSpeedWidget', 20
    ypos = AT_TOP, 'CCSpeedWidget'

    def get_rawval(self, cd, mon):
        return cd.odometer - mon.last_full_odo

    def update_rawval(self, rv):
        rv += 50
        self.set_text('%02d.%d' % (rv // 1000, (rv % 1000) // 100))

#@wjt
class RangeWidget(RangeWidgetBase):

    field = 'range'
    update_from_data = True

    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev', 5

    def get_rawval(self, cd, mon):
        return int(cd.range / DISTANCE_CONVERSION + 0.1)

    def update_rawval(self, rv):
        self.set_text('%02d' % rv)


@wjt
class DistSinceRangeChange(RangeWidgetBase):
    textsize = 32
    yo = 28
    h = 32
    w = 45

    xpos = ON_RIGHT, 'DistSinceFullWidget', 5
    ypos = AT_TOP, 'DistSinceFullWidget'

    field = 'range'
    update_from_data = True

    def get_rawval(self, cd, mon):
        if cd.range:
            return cd.odometer - mon.last_range_odo
        return 0

    def update_rawval(self, rv):
        self.set_text('%.1f' % (rv / 1000))

@wjt
class SimpleRange(RangeWidgetBase):
    textsize = 32
    yo = 28
    h = 32
    w = 80

    nchar = 8

    xpos = ON_RIGHT, 'DistSinceRangeChange', 2
    ypos = AT_TOP, 'DistSinceRangeChange'

    field = 'range'
    update_from_data = True

    def get_rawval(self, cd, mon):
        #return 140, 20000
        return cd.battery_soc, cd.odometer - mon.last_full_odo

    def update_rawval(self, rv):
        soc, dist = rv
        battpct = soc / 2.55
        if not soc:
            self.set_text('')
            return

        #print(rv, battpct, raw / 2.55)
        charge_used = 1.0 - (battpct / 100)
        if charge_used > 0.03:
            full_dist = (dist / 1000) / charge_used
            self.set_text('%.1f' % full_dist)
        else:
            self.set_text('---')

#@wjt
class RangeTrendSinceFullWidget(RangeWidgetBase):
    w = 100
    field = 'range'
    update_from_data = True

    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev', 5


    def get_rawval(self, cd, mon):
        range = int(cd.range / DISTANCE_CONVERSION + 0.1) * 1000
        return range + max(cd.odometer - 1000, mon.last_range_odo) - mon.last_full_odo

    def update_rawval(self, rv):
        rv += 50
        self.set_text('%02d.%d' % (rv // 1000, (rv % 1000) // 100))


class RangeHistoryBase(RangeWidgetBase):
    textsize = 32
    xo = 0
    yo = 28
    h = 32
    w = 40
    update_from_data = True
    fmt = '%02d'
    flags = FLAG_ALIGN_RIGHT

    def get_sample(self, mon):
        if self.idx >= len(mon.range_samples):
            return None, None, None
        return mon.range_samples[self.idx]

@wjt
class RangeTrendHistoryRange1(RangeHistoryBase):
    xpos = AT_LEFT, 'DistSinceFullWidget'
    ypos = ON_BOTTOM, 'DistSinceFullWidget', 2
    idx = 0

    def get_rawval(self, cd, mon):
        odo, range, fullrange = self.get_sample(mon)
        return range

@wjt
class RangeTrendHistoryRange2(RangeTrendHistoryRange1):
    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev', 2
    idx = 1

@wjt
class RangeTrendHistoryRange3(RangeTrendHistoryRange1):
    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev', 2
    idx = 2

@wjt
class RangeTrendHistoryRange4(RangeTrendHistoryRange1):
    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev', 2
    idx = 3

@wjt
class RangeTrendHistoryDiff1(RangeHistoryBase):
    w = 60
    idx = 0
    col = 1
    fmt = '%+.1f'

    xpos = ON_RIGHT, 'RangeTrendHistoryRange1', 3
    ypos = AT_TOP, 'RangeTrendHistoryRange1'

    def get_rawval(self, cd, mon):
        odo, range, fullrange = self.get_sample(mon)
        if range is None:
            return None
        try:
            lodo, lrange, lfullrange = mon.range_samples[self.idx + 1]
        except IndexError:
            return None
        lfullrange = (lfullrange + 50) // 100
        fullrange = (fullrange + 50) // 100
        return (fullrange - lfullrange) / 10

@wjt
class RangeTrendHistoryDiff3(RangeTrendHistoryDiff1):
    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev', 2
    idx = 1

@wjt
class RangeTrendHistoryDiff3(RangeTrendHistoryDiff1):
    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev', 2
    idx = 2

@wjt
class RangeTrendHistoryDiff4(RangeTrendHistoryDiff1):
    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev', 2
    idx = 3

@wjt
class RangeTrendHistoryFullRange1(RangeHistoryBase):
    w = 60
    idx = 0
    fmt = '%.1f'

    xpos = ON_RIGHT, 'RangeTrendHistoryDiff1', 3
    ypos = AT_TOP, 'RangeTrendHistoryDiff1'

    def get_rawval(self, cd, mon):
        odo, range, fullrange = self.get_sample(mon)
        if range is None:
            return None
        return ((fullrange + 50) // 100) / 10


@wjt
class RangeTrendHistoryFullRange2(RangeTrendHistoryFullRange1):
    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev', 2
    idx = 1

@wjt
class RangeTrendHistoryFullRange3(RangeTrendHistoryFullRange1):
    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev', 2
    idx = 2

@wjt
class RangeTrendHistoryFullRange4(RangeTrendHistoryFullRange1):
    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev', 2
    idx = 3

@wjt
class BattPctWidget(BaseWidget):
    w = 180
    h = 82
    xo = 3
    yo = 78
    textsize = 100
    xscale = .7
    nchar = 5
    field = 'battery_soc'
    update_from_data = True

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    xpos = CENTER_OF, 'SpeedWidget'
    ypos = ON_BOTTOM, 'SpeedWidget', 45

    def get_rawval(self, cd, mon):
        return cd.battery_soc, cd.battery_raw_soc, cd.range

    def update_rawval(self, rv):
        soc, raw, range = rv
        if soc == 0 or range == 0:
            battpct = raw / 2.55
            self.cfg = 0xff1111
        else:
            battpct = soc / 2.55
            self.cfg = 0xffffff

        self.set_text('%.1f' % (battpct))

@wjt
class RPMWidget(BaseWidget):
    w = 100
    h = 40
    textsize = 36
    yo = 34
    nchar = 10
    field = 'rpm'
    flags = FLAG_ALIGN_RIGHT
    update_from_data = True

    xpos = AT_RIGHT, 'SpeedWidget'
    ypos = ON_BOTTOM, 'SpeedWidget'

    def get_rawval(self, cd, mon):
        return cd.rpm, cd.hv_amps * cd.hv_volts, cd.mgb_amps * cd.mgb_volts + cd.mga_amps * cd.mga_volts

    def update_rawval(self, rv):
        rpm, hvkw, mtrkw = rv

        if rpm == 0:
            self.set_text('')
            self.cbg = 0
            hvkw /= HVKW_CONV
            mtrkw /= MOTOR_KW_CONV
            diff = '%+.1f' % (hvkw - mtrkw)
            if diff == '+0.0':
                self.cfg = 0xFFFFFF
                diff = '0.0'
            else:
                self.cfg = 0xFFFF44 if diff[0] == '-' else 0x44FF44
            self.set_text(diff)

        else:
            self.set_text('%d' % rpm)
            self.cbg = 0xFFFFDDDD
            self.cfg = 0


@wjt
class HVKWWidget(BaseWidget):
    w = 180
    h = 74
    xo = -3
    yo = 68
    textsize = 72
    xscale = .7
    flags = FLAG_ALIGN_RIGHT
    nchar = 5
    update_from_data = True

    xpos = ON_LEFT, 'SpeedWidget'
    ypos = AT_TOP, 'SpeedWidget'

    def get_rawval(self, cd, mon):
        return cd.hv_amps * cd.hv_volts

    def update_rawval(self, rv):
        if rv == 0:
            self.set_text('0.0')
            self.cfg = 0xFFFFFF
        else:
            hv_kw = rv / HVKW_CONV
            self.set_text('%+.1f' % hv_kw)
            self.cfg = 0xFFFFFF if hv_kw == 0 else (0x44FF44 if hv_kw >= 0 else 0xFFFF44)

@wjt
class MGBPwrWidget(BaseWidget):
    w = 90
    h = 36
    xo = -3
    yo = 31
    textsize = 36
    xscale = .6
    flags = FLAG_ALIGN_RIGHT
    nchar = 5
    update_from_data = True

    ypos = ON_BOTTOM, 'HVKWWidget'
    xpos = AT_LEFT, 'HVKWWidget'

    def get_rawval(self, cd, mon):
        return cd.mgb_amps * cd.mgb_volts

    def update_rawval(self, rv):
        if rv == 0:
            self.set_text('0.0')
            self.cfg = 0xFFFFFF
        else:
            hv_kw = rv / MOTOR_KW_CONV
            self.set_text('%+.1f' % hv_kw)
            self.cfg = 0xFFFFFF if hv_kw == 0 else (0x44FF44 if hv_kw >= 0 else 0xFFFF44)

@wjt
class MGBSpeedWidget(MGBPwrWidget):
    ypos = ON_BOTTOM, 'prev'
    xpos = AT_LEFT, 'prev'
    def get_rawval(self, cd, mon):
        return cd.mgb_rpm

    def update_rawval(self, rv):
        if rv == 0:
            self.set_text('--')
            self.cfg = 0xFFFFFF
        else:
            self.set_text('%+d' % rv)
            self.cfg = 0xFFFFFF if rv == 0 else (0x44FF44 if rv >= 0 else 0xFFFF44)

@wjt
class MGAPwrWidget(MGBPwrWidget):
    xpos = AT_RIGHT, 'HVKWWidget'
    def get_rawval(self, cd, mon):
        return cd.mga_amps * cd.mga_volts

@wjt
class MGASpeedWidget(MGBSpeedWidget):
    def get_rawval(self, cd, mon):
        return cd.mga_rpm

@wjt
class TripDistance(BaseWidget):
    w = 100
    h = 40
    textsize = 36
    yo = 34
    nchar = 10
    xscale = .7
    field = 'trip_distance'
    flags = FLAG_ALIGN_RIGHT
    update_from_data = True

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    xpos = ON_LEFT, 'BattPctWidget', 10
    ypos = AT_TOP, 'BattPctWidget'

    def update_rawval(self, rv):
        txt = ('%d.%03d' % (rv // 1000, rv % 1000))[:-1]
        self.set_text(txt)

@wjt
class TripEvDistance(BaseWidget):
    w = 100
    h = 40
    textsize = 36
    fg = 0xAAAAFF
    yo = 34
    nchar = 10
    xscale = .7
    field = 'trip_ev_distance'
    flags = FLAG_ALIGN_RIGHT
    update_from_data = True

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    xpos = AT_RIGHT, 'TripDistance'
    ypos = ON_BOTTOM, 'TripDistance'

    def update_rawval(self, rv):
        txt = ('%d.%03d' % (rv // 1000, rv % 1000))[:-1]
        self.set_text(txt)
@wjt
class TripGasDistance(TripEvDistance):
    xpos = ON_RIGHT, 'BattPctWidget', 10
    ypos = AT_BOTTOM, 'BattPctWidget'
    fg = 0xFFAAAA

    def get_rawval(self, cd, mon):
        return cd.trip_distance - cd.trip_ev_distance

@wjt
class ClutchStateWidget(BaseWidget):
    w = 40
    h = 36
    xo = -3
    yo = 31
    fg = 0xFFFFFF

    textsize = 36
    xscale = .6
    flags = FLAG_ALIGN_RIGHT
    nchar = 5
    update_from_data = True

    field = 'clutch_state'

    ypos = AT_BOTTOM, 'TripDistance'
    xpos = ON_LEFT, 'TripDistance'

    def update_rawval(self, rv):
        self.set_text('%02x' % rv)

@wjt
class FuelWidget(BaseWidget):
    w = 85
    h = 40
    textsize = 36
    yo = 34
    nchar = 10
    field = 'fuel_ctr'
    flags = FLAG_ALIGN_RIGHT
    update_from_data = True

    xscale = 0.7

    fg = 0xFF8888

    xpos = ON_RIGHT, 'TripGasDistance', 10
    ypos = AT_TOP, 'TripGasDistance'

    def update_rawval(self, rv):
        if rv == 0:
            self.set_text('')
        else:
            rv /= FUEL_CONVERSION
            self.set_text('%.03f' % rv)

@wjt
class MPGWidget(BaseWidget):
    w = 85
    h = 40
    textsize = 36
    yo = 34
    nchar = 10
    flags = FLAG_ALIGN_RIGHT
    update_from_data = True

    xscale = 0.7

    fg = 0xFF8888

    xpos = ON_RIGHT, 'FuelWidget', 10
    ypos = AT_TOP, 'FuelWidget'

    def get_rawval(self, cd, mon):
        fuel = cd.fuel_ctr / FUEL_CONVERSION
        if fuel < 0.001:
            return 0
        return (cd.trip_distance - cd.trip_ev_distance) / 1000 / fuel

    def update_rawval(self, rv):
        if rv == 0:
            self.set_text('')
        else:
            self.set_text('%.01f' % rv)

@wjt
class OdometerWidget(BaseWidget):
    w = 215
    h = 38
    textsize = 36
    yo = 34
    nchar = 10
    field = 'odometer'
    flags = FLAG_ALIGN_RIGHT
    update_from_data = True

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    xpos = AT_RIGHT, 'screen', 5
    ypos = AT_BOTTOM, 'screen'

    def update_rawval(self, rv):
        txt = '%d.%03d' % (rv // 1000, rv % 1000)
        #txt = '%.3f' % rv
        self.set_text(txt)

class ClimateLineWidget(BaseWidget):
    w = 32
    h = 38
    textsize = 36
    yo = 34
    vismask = VFLAG_DISPLAY_ON | VFLAG_VEHICLE_ON | VFLAG_TEXT_ENTRY
    visgroup = VFLAG_DISPLAY_ON | VFLAG_VEHICLE_ON

@wjt
class FanSpeedWidget(ClimateLineWidget):
    nchar = 1
    flags = FLAG_ALIGN_RIGHT
    field = 'select_fanspeed'
    xo = -3

    xpos = AT_LEFT, 'screen', 22
    ypos = ON_TOP, 'OdometerWidget', 5
    update_from_data = True

    def get_rawval(self, cd, mon):
        return (cd.select_fanspeed & 0xF, mon.fanspeed_target.target)

    def update_rawval(self, rv):
        cspeed, dspeed = rv
        if dspeed is not None:
            cspeed = dspeed
            self.cbg = 0xFF00aa00
        else:
            self.cbg = 0
        self.set_text('%d' % cspeed)

@wjt
class TemperatureWidget(ClimateLineWidget):
    w = 60
    xo = -5
    nchar = 2
    flags = FLAG_ALIGN_RIGHT
    field = 'select_temp'
    update_from_data = True

    xpos = ON_RIGHT, 'prev', 20
    ypos = AT_TOP, 'prev'

    def get_rawval(self, cd, mon):
        return (TemperatureTarget.convert(cd.select_temp), mon.temp_target.target)

    def update_rawval(self, rv):
        ctemp, dtemp = rv
        if dtemp is not None:
            ctemp = dtemp
            self.cbg = 0xFF00aa00
        else:
            self.cbg = 0

        if ctemp == 60:
            txt = 'Lo'
        elif ctemp == 90:
            txt = 'Hi'
        else:
            txt = '%02d' % ctemp
        self.set_text(txt)

@wjt
class ClimateModeSelWidget(ClimateLineWidget):
    w = 30
    nchar = 1
    xpos = ON_RIGHT, 'prev', 20
    ypos = AT_TOP, 'prev'

    def update(self, eco):
        self.set_text('E' if eco else 'C')
        self.cfg = 0x77FF77 if eco else 0x7777ff
        self.bump_version()

@wjt
class ClimateModeWidget(ClimateLineWidget):
    w = 90
    field = 'climate_mode'
    nchar = 3
    xpos = ON_RIGHT, 'prev', 20
    ypos = AT_TOP, 'prev'

    modes = ['CFT', 'ECO', 'FAN', '???']
    colors = [0x7777FF, 0x77FF77, 0xFFFFFF, 0xFFFFFF]
    update_from_data = True

    def update_rawval(self, rv):
        self.set_text(self.modes[rv])
        self.cfg = self.colors[rv]

@wjt
class ClimateAcWidget(ClimateLineWidget):
    field = 'heat_ac'
    nchar = 3

    xpos = ON_RIGHT, 'prev', 20
    ypos = AT_TOP, 'prev'
    update_from_data = True

    bit = 2
    char = 'A'
    fg = 0x7777FF

    def update_rawval(self, rv):
        active = rv & self.bit
        self.set_text(self.char if active else '')

@wjt
class ClimateHeatWidget(ClimateAcWidget):
    bit = 1
    char = 'H'
    xpos = ON_RIGHT, 'prev', 0
    fg = 0xFF7777
    update_from_data = True

@wjt
class ClimatePowerWidget(ClimateLineWidget):
    w = 60
    field = 'climate_power'
    nchar = 2
    update_from_data = True

    xpos = ON_RIGHT, 'prev', 20
    ypos = AT_TOP, 'prev'

    flags = FLAG_ALIGN_RIGHT

    def update_rawval(self, rv):
        self.set_text('%d' % min(99, rv))

@wjt
class MeasuredFanSpeedWidget(ClimateLineWidget):
    w = 60
    nchar = 2
    update_from_data = True

    xpos = ON_RIGHT, 'prev', 20
    ypos = AT_TOP, 'prev'

    flags = FLAG_ALIGN_RIGHT
    def get_rawval(self, cd, mon):
        return min(99, int(cd.fanspeed * 100 // 220))

@wjt
class BrakePctWidget(ClimateLineWidget):
    w = 60
    nchar = 2
    update_from_data = True

    xpos = ON_RIGHT, 'prev', 20
    ypos = AT_TOP, 'prev'

    flags = FLAG_ALIGN_RIGHT
    def get_rawval(self, cd, mon):
        return min(99, int(cd.brake_pct * 100 // 255))
@wjt
class MusicTitleWidget(BaseWidget):
    w = 590
    h = 24

    xpos = AT_LEFT, 'screen', 5
    ypos = ON_BOTTOM, 'BattPctWidget', 10

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    textsize = 24
    yo = 20
    nchar = 100

    curtitle = None
    playing = False

    def update(self):
        icon = '\u25b6' if self.playing else '\u2588'
        text = '%s %s' % (icon, self.curtitle)
        self.set_text(text)


@wjt
class MusicArtistWidget(BaseWidget):
    w = 590
    h = 18

    xpos = AT_LEFT, 'screen', 5
    ypos = ON_BOTTOM, 'MusicTitleWidget', 10

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    textsize = 16
    yo = 14
    nchar = 100

@wjt
class MusicTimeWidget(BaseWidget):
    w = 590
    h = 18

    xpos = AT_LEFT, 'screen', 5
    ypos = ON_BOTTOM, 'MusicArtistWidget', 10

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    textsize = 16
    yo = 14
    nchar = 100

    curpos = 0
    curdur = 0
    volume = 0

    def update(self):
        text = '%s / %s [%s]' % (hms(self.curpos), hms(self.curdur), self.volume)
        self.set_text(text)

@wjt
class TextEntryWidget(BaseWidget):
    w = 790
    h = 50
    font = 1
    nchar = 64

    textsize = 48
    yo = 38

    xpos = AT_LEFT, 'screen', 120
    ypos = ON_TOP, 'FanSpeedWidget', 10


@wjt
class TextEntryCursorWidget(BaseWidget):
    w = 790
    h = 50
    font = 1
    nchar = 64

    textsize = 48
    yo = 38

    fg = 0xAAFFAA
    bg = 0
    xpos = AT_LEFT, 'TextEntryWidget'
    ypos = AT_TOP, 'TextEntryWidget'

    visgroup = VFLAG_DISPLAY_ON | VFLAG_TEXT_ENTRY
    vismask = VFLAG_DISPLAY_ON | VFLAG_TEXT_ENTRY

@wjt
class ClockWidget(BaseWidget):
    w = 220
    h = 38
    xpos = AT_RIGHT, 'screen'
    ypos = AT_TOP, 'screen'
    xscale = .7
    textsize = 36
    yo = 30
    nchar = 16
    font = 0

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    lastval = None
    flags = FLAG_ALIGN_RIGHT

    def update(self):
        ctime = int(time.time())
        if ctime != self.lastval:
            self.lastval = ctime
            text = time.strftime('%m-%d %T', time.localtime(ctime))
            self.set_text(text)
            self.bump_version()

@wjt
class RecordStateWidget(BaseWidget):
    w = 72
    h = 38
    xpos = AT_RIGHT, 'screen'
    ypos = ON_BOTTOM, 'ClockWidget'
    textsize = 36
    yo = 30
    nchar = 3
    font = 1

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    lastval = None
    flags = FLAG_ALIGN_RIGHT

@wjt
class ConnectStateWidget(BaseWidget):
    w = 72
    h = 38
    xpos = ON_LEFT, 'RecordStateWidget', 10
    ypos = ON_BOTTOM, 'ClockWidget'
    textsize = 36
    yo = 30
    nchar = 3
    font = 1

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    lastval = None
    flags = FLAG_ALIGN_RIGHT

class StopTimeBase(BaseWidget):
    w = 80
    h = 38
    xscale = .7

    textsize = 36
    yo = 30
    nchar = 5

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    update_from_data = True


@wjt
class CurStopTime(StopTimeBase):
    xpos = AT_RIGHT, 'screen'
    ypos = ON_BOTTOM, 'RecordStateWidget'

    flags = FLAG_ALIGN_RIGHT

    def get_rawval(self, cd, mon):
        if mon.motion_state == STATE_STOPPED:
            return True, mon.cur_stop_time // 1000
        else:
            return False, mon.last_stop_time // 1000

    def update_rawval(self, rv):
        stopped, time = rv
        self.set_text(hms(time))
        self.cfg = 0xFFFFFF if stopped else 0x999999

@wjt
class TotalTime(StopTimeBase):
    xpos = ON_LEFT, 'prev', 15
    ypos = ON_BOTTOM, 'RecordStateWidget'

    flags = FLAG_ALIGN_RIGHT

    def get_rawval(self, cd, mon):
        return mon.total_time // 1000

    def update_rawval(self, rv):
        self.set_text(hms(rv))

@wjt
class StopTime(StopTimeBase):
    xpos = AT_LEFT, 'TotalTime'
    ypos = ON_BOTTOM, 'prev'

    flags = FLAG_ALIGN_RIGHT

    def get_rawval(self, cd, mon):
        return (mon.total_stop_time + mon.cur_stop_time) // 1000 if mon.motion_state == STATE_STOPPED else mon.total_stop_time // 1000

    def update_rawval(self, rv):
        self.set_text(hms(rv))

@wjt
class CoreTemperatureWidget(BaseWidget):
    w = 70
    h = 38
    xscale = .7
    xpos = AT_RIGHT, 'screen'
    ypos = ON_BOTTOM, 'CurStopTime'
    #bg=0xff888888
    textsize = 36
    yo = 30
    nchar = 6
    font = 0

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    lastval = None
    flags = FLAG_ALIGN_RIGHT

class TempWidgetBase(BaseWidget):
    w = 70
    h = 38
    xscale = .7
    #bg=0xff888888
    textsize = 36
    yo = 30
    nchar = 6
    font = 0

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    lastval = None
    flags = FLAG_ALIGN_RIGHT

    update_from_data = True

    bias = 40
    factor = 1

    def update_rawval(self, rv):
        f = int(.5 + (rv - self.bias) * self.factor * 1.8 + 32)
        #self.set_text('%d\xb0F' % f)
        self.set_text('%d\xb0' % f)

@wjt
class BatteryTemperatureWidget(TempWidgetBase):
    field = 'battery_temp'
    xpos = AT_RIGHT, 'screen'
    ypos = ON_BOTTOM, 'prev'
    fg = 0xAAFFAA

@wjt
class AirTemperatureWidget(TempWidgetBase):
    field = 'air_temp1'
    xpos = ON_LEFT, 'prev', 15
    ypos = AT_TOP, 'prev'
    fg = 0xFFFFAA

    factor = .5
    bias = 80

@wjt
class CoolantTemperatureWidget(TempWidgetBase):
    field = 'coolant_temp'
    xpos = AT_RIGHT, 'screen'
    ypos = ON_BOTTOM, 'prev'
    fg = 0xFFAAAA

@wjt
class BattVoltWidget(BaseWidget):
    w = 144
    h = 38
    xpos = AT_RIGHT, 'screen'
    ypos = ON_BOTTOM, 'prev'
    textsize = 36
    yo = 30
    nchar = 7
    xscale = .7
    font = 0

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    lastval = None
    flags = FLAG_ALIGN_RIGHT

    def update(self, val):
        if val != self.lastval:
            self.lastval = val
            txt = '%.2fV' % val
            self.set_text(txt)
            self.bump_version()

@wjt
class BattCurrentWidget(BaseWidget):
    w = 144
    h = 38
    xpos = AT_RIGHT, 'screen'
    ypos = ON_BOTTOM, 'prev'
    textsize = 36
    yo = 30
    nchar = 7
    xscale = .7
    font = 0

    visgroup = VFLAG_DISPLAY_ON
    vismask = VFLAG_DISPLAY_ON

    lastval = None
    flags = FLAG_ALIGN_RIGHT

    def update(self, val):
        if val != self.lastval:
            self.lastval = val
            txt = '%.0f' % val
            self.set_text(txt)
            self.bump_version()

@wjt
class DebugPidWidget(BaseWidget):
    lastval = None
    w = 180
    h = 38
    xscale = .7
    #bg=0xff888888
    textsize = 36
    yo = 30
    nchar = 12
    font = 1

    xpos = AT_RIGHT, 'screen'
    ypos = ON_BOTTOM, 'prev'
    flags = FLAG_ALIGN_RIGHT

class KeyWidget(BaseWidget):
    w = 55
    h = 55
    textsize = 48
    yo = 42
    xo = 8
    nchar = 1
    font = 1

    visgroup = VFLAG_DISPLAY_ON | VFLAG_TEXT_ENTRY
    vismask = VFLAG_DISPLAY_ON | VFLAG_TEXT_ENTRY

    bg = nsel_bg = 0xC0333333
    nsel_fg = 0xFFFFFF
    sel_bg = 0xFFFFFFFF
    sel_fg = 0

    def getkey(self):
        return self.row, self.col

    def post_build(self):
        self.set_text(self.key)
        if self.col == 4:
            self.nsel_bg = self.cbg = 0xFF666666

class BigKeyWidget(KeyWidget):
    w = 80
    h = 110
    textsize = 100
    font = 1
    yo = 90


    fg = 0xFFFFFFFF
    bg = 0xbb333333

    def getkey(self):
        return self.row, self.col

    def post_build(self):
        self.set_text(self.key)

def add_key_widget(k, x, y, r, c, sel_bg, sel_fg, cls=KeyWidget):
    @wjt
    def create(buf, pos):
        w = cls.from_buffer(buf, pos)
        w.key = k
        w.row = r
        w.col = c
        w.xpos = x
        w.ypos = y
        w.sel_bg = sel_bg
        w.sel_fg = sel_fg
        return w

def add_key_line(keys, row, sel_bg, sel_fg):
    ypos = (ON_BOTTOM, 'SpeedWidget', 5) if row == 0 else (ON_BOTTOM, (row - 1, 0))
    for col, k in enumerate(keys):
        add_key_widget(k, col * KeyWidget.w + 120, ypos, row, col, sel_bg, sel_fg)

add_key_line('1234567890', 0, BUTTON_CLR_YELLOW, 0xFF000000)
add_key_line('QWERTYUIOP', 1, BUTTON_CLR_PURPLE, 0xFF000000)
add_key_line('ASDFGHJKL_', 2, BUTTON_CLR_BLUE, 0xFF000000)
add_key_line('ZXCVBNM,.?', 3, BUTTON_CLR_GREEN, 0xFF000000)

for j in range(4):
    add_key_widget(' ', 5, BigKeyWidget.h * j, j, 10, 0, 0, BigKeyWidget)

@wjt
class OverlayWidget(BaseWidget):
    w = 650
    h = 120
    xscale = 1.3

    xpos = CENTER_OF, 'screen'
    ypos = 280
    bg = 0x70444444
    vismask = VFLAG_OVERLAY | VFLAG_DISPLAY_ON
    visgroup = VFLAG_OVERLAY | VFLAG_DISPLAY_ON

    nchar = 12
    textsize = 100
    yo = 90
    flags = FLAG_ALIGN_CENTER

class MediaWidgetBase(BaseWidget):
    curtext = None
    visgroup = VFLAG_MEDIA_INTERFACE
    vismask = VFLAG_MEDIA_INTERFACE

    nchar = 50
    bg = 0xE0444444

    def check_set_text(self, text):
        if text != self.curtext:
            self.curtext = text
            self.set_text(self.curtext or '')
            self.bump_version()

@wjt
class MediaInterfaceHeader(MediaWidgetBase):
    w = 700
    h = 26
    yo = 22
    xpos = CENTER_OF, 'screen'
    ypos = 280
    textsize = 20
    flags = 0

@wjt
class MediaInterfaceLine1(MediaWidgetBase):
    w = 700
    h = 22

    textsize = 14
    yo = 18
    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev'

@wjt
class MediaInterfaceLine2(MediaInterfaceLine1):
    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev'

@wjt
class MediaInterfaceButton1(MediaWidgetBase):
    w = 200
    h = 22

    textsize = 14
    yo = 18

    fg = 0x6666FF

    xpos = AT_LEFT, 'prev'
    ypos = ON_BOTTOM, 'prev'

@wjt
class MediaInterfaceButton2(MediaWidgetBase):
    w = 200
    h = 22

    textsize = 14
    yo = 18

    fg = 0x66FF66

    xpos = ON_RIGHT, 'prev'
    ypos = AT_TOP, 'prev'

@wjt
class MediaInterfaceButton3(MediaWidgetBase):
    w = 300
    h = 22

    textsize = 14
    yo = 18

    fg = 0xFF6666

    xpos = ON_RIGHT, 'prev'
    ypos = AT_TOP, 'prev'

class MenuWidget(BaseWidget):
    w = 266
    h = 26
    textsize = 18
    yo = 18
    xo = 8
    nchar = 30
    font = 0

    visgroup = VFLAG_MENU
    vismask = VFLAG_MENU

    bg = 0

    def getkey(self):
        return 'menu', self.idx

    def set_menu_text(self, text):
        if text != self.current_text:
            if text:
                self.cfg = self.vis_fg
                self.cbg = self.vis_bg
            else:
                self.cbg = 0
                self.cfg = 0

            self.current_text = text
            self.set_text(text)
            self.bump_version()

def add_menu_widget(x, y, idx, bg, fg):
    @wjt
    def create(buf, pos):
        w = MenuWidget.from_buffer(buf, pos)
        w.idx = idx
        w.current_text = ''
        w.vis_bg = bg
        w.vis_fg = fg

        w.ypos = 480 + (y - 4) * MenuWidget.h
        w.xpos = x * MenuWidget.w
        return w

def setup_menu():
    for i in range(4):
        add_menu_widget(0, i, i, 0xCC333333, 0xFFFFFF)

    for i in range(4):
        add_menu_widget(1, i, i + 4, MENU_COLOR_BG[i], MENU_COLOR_FG[i])

    for i in range(4):
        add_menu_widget(2, i, i + 8, 0xCC333333, 0xFFFFFF)

setup_menu()

def debug_widget_config():
    from PIL import Image, ImageDraw
    buf = bytearray(32768)
    wc = WidgetConfig(buf)
    wc.build(all_widgets, 800, 480)
    im = Image.new('RGB', (800, 480))
    ctx = ImageDraw.Draw(im)

    for w in wc.widgets:
        if not w.visgroup & (VFLAG_TEXT_ENTRY|VFLAG_OVERLAY):
            pass

if __name__ == '__main__':
    debug_widget_config()
