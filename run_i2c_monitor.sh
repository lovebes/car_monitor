#!/bin/sh

cd `dirname $0`
echo start i2c monitor
export PYTHONUNBUFFERED=1
exec ./i2c_monitor.py > logpipe 2>&1
