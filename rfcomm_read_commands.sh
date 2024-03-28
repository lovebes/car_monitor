#!/bin/bash
rfcomm_dev=$1
exec < $rfcomm_dev
while read j; do
    echo "send command $j" >&2
    echo -n $j > /dev/udp/127.0.0.1/9900
done
