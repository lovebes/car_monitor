#!/usr/bin/python3
import sys
import re
import time
import os
import argparse
import subprocess
import traceback
from os.path import dirname, basename, join, exists, expanduser

from utils import getmtime

import i2c_shmem

MCP23017_ADDR = 0x20
PCF8574_ADDR = 0x27

IOCON = 0x0A
IODIRA = 0x00
GPIOA = 0x12

try:
    import Adafruit_GPIO.I2C as I2C
    from ina219 import INA219
except ImportError:
    I2C = None
    INA219 = None

def main():
    p = argparse.ArgumentParser(description='')
    p.add_argument('-d', '--dump', action='store_true', help='')
    p.add_argument('-s', '--setpin', nargs=2, type=int, help='')
    p.add_argument('-b', '--bus', default=1, type=int, help='')
    #p.add_argument(help='')
    args = p.parse_args()

    data = i2c_shmem.I2CData.create(i2c_shmem.PATH)

    pe = data.pin_enable

    if args.dump:
        print('v = %6.3f V' % (data.volts / 1000))
        print('c = %6d mA' % (data.current))
        return

    if args.setpin:
        pe[args.setpin[0]] = args.setpin[1]
        return


    ina = INA219(
        busnum=args.bus,
        shunt_ohms=0.1,
        max_expected_amps = 2.0,
        address=0x40)

    try:
        ina.configure(voltage_range=ina.RANGE_16V,
                      gain=ina.GAIN_AUTO,
                      bus_adc=ina.ADC_2SAMP,
                      shunt_adc=ina.ADC_2SAMP)
    except Exception:
        print('Warning: could not configure INA219!')
        ina = None

    gpio = I2C.get_i2c_device(address=PCF8574_ADDR, busnum=args.bus)

    lastval = [None] * 8
    last_dirbits = 0

    pwr_press_time = None
    shutdown_enable_time = 0

    while True:
        if ina:
            try:
                volt = ina.supply_voltage()
                # New INA219 swapped VIN- and VIN+. Don't feel like resoldering the board.
                current = -ina.current()
                data.volts = int(volt * 1000)
                data.current = int(current)
            except Exception:
                data.volts = 0
                data.current = 0
                pass

        inp = gpio.readRaw8()
        data.pin_input = inp
        ctime = time.time()
        if not (inp & 0x40):
            if pwr_press_time is None:
                pwr_press_time = ctime

            if pwr_press_time < shutdown_enable_time and ctime - pwr_press_time >= 2.0:
                shutdown_enable_time = 0
                os.system('./do-shutdown&')
        else:
            if pwr_press_time:
                shutdown_enable_time = ctime + 5
                pwr_press_time = None

        changes = False

        for i in range(8):
            v = pe[i]
            if v != lastval[i]:
                lastval[i] = v
                changes = True


        if changes:
            # Never turn off pin 5 - only shutdown script does that
            valbits = 0x10
            for i in range(8):
                v = lastval[i]
                if v == 0:
                    valbits |= 1 << i
            try:
                gpio.writeRaw8(valbits)
            except Exception:
                traceback.print_exc()

        time.sleep(0.1)





if __name__ == '__main__':
    main()
