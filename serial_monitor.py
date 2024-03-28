#!/usr/bin/python3
import sys
import re
import time
import os
import pty

import termios

import serial
import select
import socket
import argparse
import subprocess
import traceback
import datetime
import signal

from os.path import dirname, basename, join, exists
from select import EPOLLIN, EPOLLOUT

from utils import load_config, CONFIG, getmtime
from bitstream import BitStream

import hotload
import monitor_hotload

hotload.initreload(monitor_hotload)

SOH = b'\x01'
STX = b'\x02'
ETX = b'\x03'

def get_base_termios(speed):
    return [0, termios.ONLCR | termios.OPOST, 3261, 2608, speed, speed, [
        b'\x00',  #  0 VINTR
        b'\x00',  #  1 VQUIT
        b'\x00',  #  2 VERASE
        b'\x00',  #  3 VKILL
        b'\x00',  #  4 VEOF
        0,        #  5 VTIME
        1,        #  6 VMIN
        b'\x00',  #  7 VSWTCH
        b'\x00',  #  8 VSTART
        b'\x00',  #  9 VSTOP
        b'\x00',  # 10 VSUSP
        b'\x02',  # 11 VEOL
        b'\x00',  # 12 VREPRINT
        b'\x0f',  # 13 VDISCARD
        b'\x17',  # 14 VWERASE
        b'\x16',  # 15 VLNEXT
        b'\x00',  # 16 VEOL2
        b'\x00', b'\x00', b'\x00', b'\x00', b'\x00', b'\x00', b'\x00', b'\x00', b'\x00', b'\x00', b'\x00', b'\x00', b'\x00', b'\x00', b'\x00']
    ]

def setup_serial(fd, speed):
    termattr_raw = get_base_termios(speed)
    termios.tcsetattr(fd, termios.TCSANOW, termattr_raw)

def setup_serial_canon(fd, speed):
    termattr_raw = get_base_termios(speed)
    termattr_raw[3] |= termios.ICANON
    termios.tcsetattr(fd, termios.TCSANOW, termattr_raw)

BLUETOOTH_DISCONNECTED, BLUETOOTH_CONNECTIONG, BLUETOOTH_CONNECTED = range(3)

class SerialMonitor:
    def __init__(self, args, term_fd, shell_fd):
        self.args = args
        self.shell_fd = shell_fd
        self.term_fd = term_fd

        self.log('=== serial monitor active ===')
        self.send(b'monitor active\n')
        udpport = args.port

        self.all_subprocesses = []


        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', udpport))

        gpio_poll_bin = join(dirname(__file__), args.poller)
        gpio_poll_conf = join(dirname(__file__), 'gpio_poll.conf')
        self.gpio_poll = subprocess.Popen([gpio_poll_bin, '-c', gpio_poll_conf], stdout=subprocess.PIPE)

        self.read_funcs = {}

        self.want_bluetooth = False
        self.bluetooth_process = None
        self.bluetooth_fd = None
        self.bt_outbuf = bytearray()


        self.bt_query_buffer = bytearray()
        self.bt_data_buffer = bytearray()
        self.bt_in_query = False
        self.bt_telesc = False

        self.poller = select.epoll()
        #print('term fd = %d' % term_fd)
        self.poller.register(self.term_fd, EPOLLIN)
        self.register_fd(self.sock, self.read_sock)
        self.register_fd(shell_fd, self.read_shell)
        self.register_fd(self.gpio_poll.stdout, self.read_gpio)

        self.verbose_dbg = False

        self.shell_outbuf = bytearray()
        monitor_hotload.init(self)


    def bluetooth_push(self):
        while self.bt_outbuf:
            try:
                nw = os.write(self.bluetooth_fd, self.bt_outbuf)
                del self.bt_outbuf[:nw]
            except BlockingIOError:
                self.poller.modify(self.bluetooth_fd, EPOLLIN | EPOLLOUT)
                return
            except IOError:
                self.disconnect_bluetooth()
                self.connect_bluetooth()
                return
        self.poller.modify(self.bluetooth_fd, EPOLLIN)

    def bluetooth_write(self, dat):
        self.bt_outbuf.extend(dat)
        self.bluetooth_push()

    def register_subprocess(self, proc):
        self.all_subprocesses.append(proc)

    def start_subprocess(self, *a, **kw):
        proc = subprocess.Popen(*a, **kw)
        self.register_subprocess(proc)
        return proc

    def check_subprocesses(self):
        procs = self.all_subprocesses
        i = 0
        while i < len(procs):
            p = procs[i]
            if p.poll() is not None:
                del procs[i]
            else:
                i += 1

    def disconnect_bluetooth(self):
        if self.bluetooth_fd is not None:
            try:
                self.unregister_fd(self.bluetooth_fd)
            except OSError:
                pass
            os.close(self.bluetooth_fd)
            self.bluetooth_fd = None

        if self.bluetooth_process is not None:
            if self.bluetooth_process.returncode is None:
                os.kill(self.bluetooth_process.pid, signal.SIGTERM)
            self.bluetooth_process = None

    def connect_bluetooth(self):
        if not self.want_bluetooth:
            return

        if self.bluetooth_process is None:
            self.bluetooth_process = self.start_subprocess(['./connect_spp.sh'], stdout=subprocess.PIPE)
            self.register_fd(self.bluetooth_process.stdout.fileno(), self.read_bt_process)
            self.log('started spp connect')

    def set_bluetooth(self, on):
        self.want_bluetooth = on
        if on:
            self.connect_bluetooth()
        else:
            self.disconnect_bluetooth()


    def read_bt_process(self, fd, ev):
        data = os.read(fd, 256)
        self.log('spp process: %r' % data)
        if not data:
            try:
                self.unregister_fd(fd)
            except OSError:
                pass
            self.disconnect_bluetooth()
            self.connect_bluetooth()
            return

        if b'hangup' in data:
            if self.bluetooth_fd is None:
                self.bluetooth_fd = os.open('/dev/rfcomm0', os.O_RDWR | os.O_NONBLOCK)
                del self.bt_outbuf[:]
                setup_serial(self.bluetooth_fd, termios.B4000000)
                self.register_fd(self.bluetooth_fd, self.bluetooth_read)

    def bluetooth_read(self, fd, ev):
        if ev & EPOLLOUT:
            self.bluetooth_push()

        if ev & EPOLLIN:
            try:
                data = os.read(fd, 4096)
            except BlockingIOError:
                return

            if not data:
                try:
                    self.poller.unregister(fd)
                except OSError:
                    pass
                self.disconnect_bluetooth()
                self.connect_bluetooth()
                return

            telesc = self.bt_telesc
            in_query = self.bt_in_query
            qbuf = self.bt_query_buffer
            dbuf = self.bt_data_buffer

            #self.log('bt data: %r' % data)
            for byte in data:
                if byte == 1:
                    in_query = True
                    del qbuf[:]
                elif byte == 3:
                    #self.log('qbuf at etx: %r' % qbuf)
                    if in_query and len(qbuf) > 0:
                        self.sendq(qbuf)

                    in_query = False
                elif byte == 4:
                    telesc = True
                elif in_query:
                    qbuf.append(byte)
                else:
                    if telesc:
                        if byte > 64:
                            byte -= 64
                        telesc = False
                    dbuf.append(byte)

            if dbuf:
                os.write(self.shell_fd, dbuf)
                del dbuf[:]

    def register_fd(self, fd, func):
        if not isinstance(fd, int):
            fd = fd.fileno()
        #print('register fd %d -> %r' % (fd, func))
        self.read_funcs[fd] = func
        self.poller.register(fd, EPOLLIN)

    def unregister_fd(self, fd):
        if not isinstance(fd, int):
            fd = fd.fileno()
        try:
            del self.read_funcs[fd]
        except KeyError:
            pass

        try:
            self.poller.unregister(fd)
        except OSError:
            pass

    def read_sock(self, fd, ev):
        pkt, addr = self.sock.recvfrom(256)
        self.sendq(pkt)

    def try_call(self, f, *args):
        try:
            return getattr(monitor_hotload, f)(self, *args)
        except Exception as e:
            self.log('exception calling %s' % f)
            traceback.print_exc()

    def read_gpio(self, fd, ev):
        evt = os.read(fd, 1)
        if evt:
            evt = evt[0]
            self.try_call('gpio_event', evt)

    def read_shell(self, fd, ev):
        outbuf = self.shell_outbuf
        del outbuf[:]
        data = os.read(fd, 256)
        for byte in data:
            if byte < 5:
                outbuf.append(4)
                byte += 64
            outbuf.append(byte)
        #self.log('shell out: %r' % data)
        #os.write(1, outbuf)
        if self.bluetooth_fd:
            self.bluetooth_write(outbuf)

    def log(self, txt):
        print(txt)

    def send(self, txt):
        os.write(self.term_fd, txt)

    def sendq(self, txt):
        if isinstance(txt, str):
            txt = txt.encode('utf8')

        if txt[0] == 77:
            if self.bluetooth_fd is not None:
                self.bluetooth_write(SOH + txt[1:] + ETX)
        elif txt[0] == 109:
            txt = txt.decode('utf8', 'ignore')
            msgtype = txt[1:2]
            msgtext = txt[2:]
            self.try_call('parse_message', msgtype, msgtext)
        else:
            self.send(SOH + txt + ETX)

    def parse_frame(self, bs):
        self.try_call('parse_frame', bs)

    def run(self):
        in_frame = False
        bs = BitStream()

        #bs.dbg = self.log
        outbuf = self.shell_outbuf
        telesc = False

        sfd = 0
        poll = self.poller
        etx_count = 0

        next_tick = 0

        while True:
            ctime = getmtime()
            wtime = max(0, next_tick - ctime)
            if wtime == 0:
                try:
                    mod, reloaded = hotload.tryreload(monitor_hotload, report_error=False)
                except Exception:
                    self.log('exception loading module')
                    traceback.print_exc()
                    reloaded = False

                if reloaded:
                    self.log('reloaded module')
                    self.try_call('init')
                self.try_call('tick')

                self.check_subprocesses()
                next_tick += 0.25
                if next_tick <= ctime:
                    next_tick = ctime + 0.25

            events = poll.poll(wtime)
            for fd, event in events:
                if fd == self.term_fd:
                    data = os.read(fd, 256)
                    ldata = len(data)
                    pos = 0
                    while pos < ldata:
                        frame, pos = bs.parse_data(data, pos, ldata - pos)
                        if frame:
                            if self.bluetooth_fd is not None:
                                buf = bs.getbuffer()
                                bbuf = self.bt_outbuf
                                bbuf.append(2)
                                bbuf.extend(buf)
                                bbuf.append(3)
                                self.bluetooth_push()

                            bs.unpack_15()
                            read_crc = bs.read_bits(15)
                            calc_crc = bs.calc_crc()
                            if read_crc == calc_crc:
                                self.parse_frame(bs)
                            else:
                                buf = bs.getbuffer()
                                self.log('crc error: read %04x, calc %04x: %r' % (read_crc, calc_crc, buf))

                else:
                    func = self.read_funcs.get(fd)
                    if func:
                        func(fd, event)

    def stop(self):
        self.gpio_poll.terminate()
        if self.bluetooth_process and self.bluetooth_process.returncode is None:
            os.kill(self.bluetooth_process.pid, signal.SIGTERM)


def handle_sigterm(*a):
    raise KeyboardInterrupt

def main():
    p = argparse.ArgumentParser(description='')
    p.add_argument('-P', '--port', type=int, default=9900)
    p.add_argument('-t', '--term')
    p.add_argument('-g', '--poller', default='gpio_poll')

    args = p.parse_args()

    load_config()

    mydir = dirname(__file__)
    os.chdir(mydir)

    shell_pid, shell_fd = pty.fork()
    if shell_pid == 0:
        os.chdir(mydir)
        os.environ['debian_chroot'] = 'serial'
        os.environ['TERM'] = 'xterm'
        os.environ['HOME'] = '/root'

        try:
            while True:
                if os.getuid() == 0:
                    subprocess.call(['/bin/login', '-f', 'root'])
                else:
                    subprocess.call(['/bin/bash'])
        finally:
            os._exit(1)

    if args.term:
        if args.term == 'pty':
            termfd, slavefd = pty.openpty()
            print('pty = %s' % os.ttyname(slavefd))
        else:
            termfd = os.open(args.term, os.O_RDWR)

    else:
        termfd = 1

    origattr = termios.tcgetattr(termfd)
    setup_serial_canon(termfd, termios.B38400)

    signal.signal(signal.SIGTERM, handle_sigterm)



    mon = SerialMonitor(args, termfd, shell_fd)
    try:
        mon.run()
    except Exception as e:
        traceback.print_exc()
    finally:
        termios.tcsetattr(termfd, termios.TCSANOW, origattr)
        mon.stop()

if __name__ == '__main__':
    main()
