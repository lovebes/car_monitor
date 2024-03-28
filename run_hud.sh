#!/bin/sh

cd `dirname $0`
export PYTHONUNBUFFERED=1

# Disable kernel message printing (except for panic/emergency)
dmesg -n 1
#/bin/echo -ne '\033[?25l' > /dev/tty1
exec ./hud -v 7 > logpipe 2>&1
