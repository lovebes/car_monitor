#!/bin/sh

cd `dirname $0`
echo start clock monitor
export PYTHONUNBUFFERED=1
exec ./clock_monitor.py > logpipe 2>&1
