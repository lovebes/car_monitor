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

from utils import load_config, CONFIG, getmtime, setup_gpio, set_gpio

import i2c_shmem

GPIO_ACT = 20

DISK_PATH_BASE = '/dev/disk/by-label/'
MOUNT_PATH_BASE = '/media/autocopy/'

NOT_MOUNTED, MOUNT_IN_PROGRESS, MOUNTED, UNMOUNT_IN_PROGRESS = range(4)

def set_led(val):
    set_gpio(GPIO_ACT, val)
    try:
        with open('/sys/class/leds/led0/brightness', 'w') as fp:
            fp.write('0' if val else '1')
    except OSError:
        pass

def send_serial_info(txt):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(txt.encode('utf8'), ('127.0.0.1', 9900))

def flag_prop(flag):
    def get(self):
        return self.check_flag(flag)

    def set(self, val):
        self.set_flag(flag, val)

    return property(get, set, None, flag)

class Manager:
    def __init__(self):
        self.curstate = IdleStateFlash()
        self.timeout_time = None

        self.flag_path = '.'

        self.i2c_data = i2c_shmem.I2CData.create(i2c_shmem.PATH)

        self.all_subprocesses = []
        self.led_flash_base = 0
        self.led_flash_period = 1
        self.cur_led_state = None
        self.set_state(self.curstate)
        self.cameras = []
        for i, cam in enumerate(CONFIG['cameras']):
            self.cameras.append(Camera(i, DISK_PATH_BASE + cam['label'], cam['mountpath'], cam.get('copyname', None), cam.get('sequential', False), cam.get('forcetz', None), cam.get('storage', False)))

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

    want_record = flag_prop('want-record')
    _want_mount = flag_prop('want-mount')
    _want_mount_cam = flag_prop('want-mount-cam')
    _want_mount_ext = flag_prop('want-mount-ext')
    want_lcopy = flag_prop('want-lcopy')
    want_wifi = flag_prop('want-wifi')
    need_check = flag_prop('need-check')
    copy_restart = flag_prop('copy-restart')
    abort_all = flag_prop('abort-all')
    reset_hub = flag_prop('reset-hub')
    wifi_ready = flag_prop('dbg-wifi-ready')

    @property
    def want_mount(self):
        return self._want_mount or self._want_mount_cam or self._want_mount_ext

    @property
    def want_mount_cam(self):
        return self._want_mount or self._want_mount_cam

    @property
    def want_mount_ext(self):
        return self._want_mount or self._want_mount_ext

    def register_subprocess(self, proc):
        self.all_subprocesses.append(proc)

    def start_subprocess(self, *a, **kw):
        proc = subprocess.Popen(*a, **kw)
        self.register_subprocess(proc)
        return proc

    def send_notify(self, status):
        url = CONFIG['upload_notify_url'].format(status=status, copyname='', key=CONFIG['key'])
        args = ['curl', '-s', url]
        self.start_subprocess(args)

    def check_subprocesses(self):
        procs = self.all_subprocesses
        i = 0
        while i < len(procs):
            p = procs[i]
            if p.poll() is not None:
                del procs[i]
            else:
                i += 1

    def update_led_state(self):
        rv = 0

        ctime = (getmtime() - self.led_flash_base) % self.led_flash_period
        cval = False
        for v in self.curstate.led_flash:
            ctime -= v
            if ctime <= 0:
                rv = -ctime
                break
            cval = not cval

        if cval != self.cur_led_state:
            set_led(cval)
            self.cur_led_state = cval

        return rv

    def set_state(self, newstate):
        oldstate = self.curstate
        oldstate.exit(self, newstate)

        print('change state %s -> %s' % (oldstate, newstate))
        for j in range(2):
            send_serial_info('CS=%s' % newstate.state_code)
        with open(join(self.flag_path, 'state'), 'w') as fp:
            fp.write('%s\n' % newstate.state_code)
        self.curstate = newstate

        self.led_flash_period = sum(newstate.led_flash)
        self.led_flash_base = getmtime()

        cmd = newstate.get_external_command(self)
        if cmd:
            newstate.external_process = self.start_subprocess(cmd)

        self.i2c_data.enable_hub(newstate.gpio_hub_power)
        self.i2c_data.enable_dc1(newstate.gpio_dc_power)
        self.i2c_data.enable_dc2(newstate.gpio_dc_power)

        if newstate.want_wifi != oldstate.want_wifi:
            self.want_wifi = newstate.want_wifi

        if newstate.timeout:
            self.timeout_time = getmtime() + newstate.timeout
        else:
            self.timeout_time = None

        newstate.enter(self, oldstate)
        return True

    def check_state(self):
        self.check_subprocesses()

        state = self.curstate
        if state.external_process:
            rcode = state.external_process.returncode
            if rcode is not None:
                state.external_process = None
                newstate = state.process_complete(self, rcode)
                if newstate:
                    return self.set_state(newstate)

        for cam in self.cameras:
            cam.check(self)

        newstate = state.check_transition(self)
        if newstate:
            return self.set_state(newstate)

        if self.timeout_time and getmtime() > self.timeout_time:
            newstate = state.on_timeout(self)
            if newstate:
                return self.set_state(newstate)

    def run(self):
        while True:
            if not self.check_state():
                sleeptime = min(0.2, self.update_led_state())
                time.sleep(sleeptime)

class Camera:
    def __init__(self, index, disk_path, mount_path, output_name, sequential, forcetz=None, storage=False):
        self.index = index
        self.disk_path = disk_path
        self.mount_path = mount_path
        self.output_name = output_name
        self.sequential = sequential
        self.forcetz = forcetz
        self.storage = storage

        self.timeout = 0
        self.mount_rw = False
        self.want_mount = False
        self.mount_state = NOT_MOUNTED

        self.mount_process = None
        self.unmount_process = None

    def check(self, mgr):
        ctime = getmtime()
        if self.mount_state == NOT_MOUNTED:
            if self.want_mount:
                self.timeout = ctime + 40
                print('%s: waiting for USB' % self.output_name)
                self.mount_state = MOUNT_IN_PROGRESS

        elif self.mount_state == MOUNTED:
            if not self.want_mount:
                self.timeout = ctime + 15
                print('%s: unmount in progress' % self.output_name)
                self.mount_state = UNMOUNT_IN_PROGRESS

        if self.mount_state == MOUNT_IN_PROGRESS:
            if ctime > self.timeout:
                if self.mount_process is not None:
                    self.mount_process.send_signal(signal.SIGINT)

                print('%s: mount timed out' % self.output_name)
                self.state_time = ctime
                self.want_mount = False
                self.mount_state = NOT_MOUNTED
                return

            if self.mount_process is None: # Waiting for USB device
                if self.storage_ready():
                    self.timeout = ctime + 15
                    print('%s: mounting' % self.output_name)
                    cmd = ['./do_mount.sh', self.disk_path, self.mount_path, str(int(self.mount_rw))]
                    self.mount_process = mgr.start_subprocess(cmd)
                else:
                    if not self.want_mount:
                        self.mount_state = NOT_MOUNTED
            else: # Waiting for mount process
                rc = self.mount_process.returncode
                if rc is not None:
                    self.mount_process = None
                    print('%s: mount script status = %d' % (self.output_name, rc))
                    if rc == 0:
                        self.mount_state = MOUNTED
                    else:
                        self.want_mount = False
                        self.mount_state = NOT_MOUNTED

        elif self.mount_state == UNMOUNT_IN_PROGRESS:
            if ctime > self.timeout:
                print('%s: unmount timed out' % (self.output_name))
                if self.unmount_process is not None:
                    self.unmount_process.send_signal(signal.SIGINT)
                    self.unmount_process = None
                self.mount_state = NOT_MOUNTED
                return

            if self.unmount_process:
                rc = self.unmount_process.returncode
                if rc is None:
                    return
                print('%s: unmount script status = %d' % (self.output_name, rc))
                if rc == 0:
                    self.unmount_process = None
                    self.mount_state = NOT_MOUNTED
                    return

            print('%s: unmounting' % self.output_name)
            cmd = ['./do_unmount.sh', self.disk_path, self.mount_path]
            self.unmount_process = mgr.start_subprocess(cmd)

    def storage_ready(self):
        return exists(self.disk_path) or exists('dbg-storage-ready')

class CopyOpts:
    def __init__(self, local=False):
        self.local = local

class State:
    gpio_dc_power = False
    gpio_hub_power = True
    want_wifi = True
    state_code = ''

    led_flash = [1]

    external_process = None
    external_command = None
    timeout_state = None

    timeout = None

    def stop_process(self):
        if self.external_process:
            self.external_process.send_signal(signal.SIGINT)

    def enter(self, mgr, oldstate):
        pass

    def exit(self, mgr, newstate):
        pass

    def get_external_command(self, mgr):
        return self.external_command

    def process_complete(self, mgr, rcode):
        pass

    def on_timeout(self, mgr):
        self.stop_process()
        return self.timeout_state()

    def check_transition(self, mgr):
        return None

    def __str__(self):
        return type(self).__name__

class IdleState(State):
    state_code = 'IDL'
    led_flash = [7.9, .1]
    want_wifi = False

    def check_transition(self, mgr):
        if mgr.abort_all:
            mgr.need_check = False
            mgr._want_mount = False
            mgr._want_mount_cam = False
            mgr._want_mount_ext = False
            mgr.abort_all = False
            mgr.want_lcopy = False

        if mgr.reset_hub:
            mgr.reset_hub = False
            return ResetHubState()

        if mgr.want_mount:
            return ManualMountExtState()

        if mgr.want_record:
            return RecordState()

        if mgr.need_check:
            return WifiWaitState()

        if mgr.want_lcopy:
            for cam in mgr.cameras:
                cam.mount_rw = cam.storage
                cam.want_mount = True
            return WaitMountState(0, CopyOpts(local=True))

class IdleStateFlash(IdleState):
    led_flash = [0, 1, .1, .1, .1, .1, .5]
    timeout = sum(led_flash)
    timeout_state = IdleState

class ResetHubState(State):
    state_code = 'HUB'
    gpio_hub_power = False
    want_wifi = False
    timeout_state = IdleState
    timeout = 3

class RecordState(State):
    state_code = 'REC'
    gpio_dc_power = True
    gpio_hub_power = False
    want_wifi = False

    led_flash = [1, 1]

    def enter(self, mgr, oldstate):
        mgr.need_check = True
        with open('record_start_time', 'w') as fp:
            ctime = time.time()
            cmtime = getmtime()
            self.beep_time = cmtime + 17
            fp.write('%r\n%r\n' % (ctime, cmtime))

    def exit(self, mgr, ns):
        try:
            os.unlink('record_start_time')
        except OSError:
            pass

    def check_transition(self, mgr):
        if mgr.abort_all or mgr.want_mount or not mgr.want_record:
            return PoweroffWaitInhibitState()

        if self.beep_time and getmtime() >= self.beep_time:
            send_serial_info('mB')
            self.beep_time = None

class PoweroffWaitState(State):
    state_code = 'PWR'
    timeout = 4
    gpio_hub_power = False
    timeout_state = IdleStateFlash
    led_flash = [.9, .1]
    want_wifi = False

class PoweroffWaitInhibitState(State):
    state_code = 'PWR'
    timeout = 9

    timeout_state = IdleStateFlash
    led_flash = [.9, .1]

class WifiWaitState(State):
    state_code = 'WFS'

    timeout = 60
    timeout_state = IdleStateFlash
    led_flash = [.5, .5]

    def check_transition(self, mgr):
        if mgr.abort_all or mgr.want_mount or mgr.want_record:
            return IdleStateFlash()

        try:
            with open('wifi-addr') as fp:
                addr = fp.read().strip()
        except FileNotFoundError:
            addr = None

        path = CONFIG['cardata_path']
        if addr:
            mgr.start_subprocess(['./do_copy.py', '--nonotify', '--cardata', path, 'cardata'])
            for cam in mgr.cameras:
                if not cam.storage:
                    cam.mount_rw = False
                    cam.want_mount = True

            return WaitMountState(0, CopyOpts(local=False))

    def on_timeout(self, mgr):
        mgr.need_check = False
        return super().on_timeout(mgr)

class IndexState(State):
    def __init__(self, idx, opts):
        self.camera_index = idx
        self.opts = opts

    def __str__(self):
        return '%s(%d)' % (type(self).__name__, self.camera_index)

class WaitMountState(IndexState):
    state_code = 'USB'
    gpio_dc_power = True
    led_flash = [1, .5]

    def check_transition(self, mgr):
        if mgr.want_record or mgr.abort_all:
            return WaitUnmountState()

        if self.camera_index >= len(mgr.cameras):
            if self.opts.local:
                mgr.want_lcopy = False
            else:
                mgr.need_check = False
            return WaitUnmountState()

        cam = mgr.cameras[self.camera_index]
        if cam.storage or mgr.check_flag('nocopy-%d' % self.camera_index):
            return WaitMountState(self.camera_index + 1, self.opts)

        if cam.mount_state == MOUNTED:
            return RunCopyState(self.camera_index, self.opts)
        elif cam.mount_state == NOT_MOUNTED:
            mgr.need_check = False
            mgr.send_notify('usbfail')
            return WaitUnmountState()

class RunCopyState(IndexState):
    state_code = 'CPY'
    gpio_dc_power = True
    led_flash = [0, 1000]

    def get_external_command(self, mgr):
        cam = mgr.cameras[self.camera_index]
        args = ['./do_copy.py', cam.mount_path + '/DCIM/MOVIE', cam.output_name]
        if self.opts.local:
            args.extend(['--nonotify', '--localpath', CONFIG['extra_storage']])

        if cam.sequential:
            args.append('--sequential')

        if cam.forcetz is not None:
            args.append('--forcetz')
            args.append(str(cam.forcetz))

        return args

    def enter(self, mgr, oldstate):
        pass

    def check_transition(self, mgr):
        if mgr.want_record or mgr.abort_all:
            mgr.send_notify('abort')
            self.stop_process()
            return WaitUnmountState()

        if mgr.copy_restart:
            mgr.copy_restart = False
            mgr.send_notify('abort')
            self.stop_process()
            return RunCopyState(self.camera_index, self.opts)

    def process_complete(self, mgr, rcode):
        if rcode == 0:
            return WaitMountState(self.camera_index + 1, self.opts)
        else:
            mgr.send_notify('fail')
            return WaitUnmountState()

class ManualMountExtState(State):
    state_code = 'MAN'
    gpio_dc_power = False
    led_flash = [0, 1000]

    def enter(self, mgr, oldstate):
        for cam in mgr.cameras:
            cam.mount_rw = True
            cam.want_mount = cam.storage

    def check_transition(self, mgr):
        if not mgr.want_mount or mgr.abort_all:
            return WaitUnmountExtState()

        if mgr.want_mount_cam:
            return ManualMountCamState()

class ManualMountCamState(State):
    state_code = 'MAN'
    gpio_dc_power = True
    led_flash = [0, 1000]

    def check_transition(self, mgr):
        if not mgr.want_mount or mgr.abort_all:
            return WaitUnmountState()

        for cam in mgr.cameras:
            cam.mount_rw = True
            cam.want_mount = mgr.want_mount_ext if cam.storage else mgr.want_mount_cam

class WaitUnmountState(State):
    state_code = 'UMT'
    gpio_dc_power = True
    next_state = PoweroffWaitState
    led_flash = [.9, .1]

    def enter(self, mgr, oldstate):
        for cam in mgr.cameras:
            cam.want_mount = False

    def check_transition(self, mgr):
        for cam in mgr.cameras:
            if cam.mount_state != NOT_MOUNTED:
                return None
        return self.next_state()

class WaitUnmountExtState(WaitUnmountState):
    next_state = IdleStateFlash
    gpio_dc_power = False

def main():
    p = argparse.ArgumentParser(description='')
    p.add_argument('files', nargs='*', help='files')
    p.add_argument('-p', '--pidfile', help='')
    p.add_argument('-l', '--logfile', help='')
    args = p.parse_args()

    load_config()

    setup_gpio(GPIO_ACT)

    try:
        with open('/sys/class/leds/led0/trigger', 'w') as fp:
            fp.write('none')
    except OSError:
        pass

    ctime = time.strftime('%F %T', time.localtime(time.time()))
    print('============ dashcam_monitor started ============')
    mgr = Manager()
    mgr.run()

if __name__ == '__main__':
    main()
