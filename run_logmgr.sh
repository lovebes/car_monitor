#!/bin/sh

cd `dirname $0`
echo start logmgr
exec ./logmgr.py logpipe dclogs/log-@@.txt -l today yesterday
