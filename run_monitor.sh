#!/bin/sh

cd `dirname $0`
echo start monitor
export PYTHONUNBUFFERED=1
exec ./dashcam_monitor.py > logpipe 2>&1
