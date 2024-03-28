#!/bin/sh

cd `dirname $0`
export PYTHONUNBUFFERED=1
exec ./serial_monitor.py -t /dev/ttyS0 > logpipe 2>&1
