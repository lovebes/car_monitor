#!/bin/sh

cd `dirname $0`
echo start wifi monitor
export PYTHONUNBUFFERED=1
exec ./wifi_monitor.py > logpipe 2>&1
