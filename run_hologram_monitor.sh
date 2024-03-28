#!/bin/sh
cd `dirname $0`
export PYTHONUNBUFFERED=1
exec ./hologram_monitor.py > logpipe 2>&1
