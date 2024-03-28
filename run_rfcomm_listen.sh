#!/bin/sh
cd `dirname $0`
while ! sdptool add sp; do sleep 1; done
exec rfcomm watch rfcomm1 1 ./rfcomm_read_commands.sh {}
