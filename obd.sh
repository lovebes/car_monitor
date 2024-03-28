#!/bin/bash

cd `dirname $0`
mod=$1
printf -v pid '%04X' 0x$2
outf=obd-$pid-$mod.txt
rm -f $outf

echo -n W0622 > /dev/udp/127.0.0.1/9900
sleep .2

echo O${mod}${pid} > /dev/udp/127.0.0.1/9900

for t in {0..100}; do
    if [ -e $outf ]; then
        cat $outf
        exit
    fi
    sleep .1
done
