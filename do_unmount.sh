#!/bin/bash

IFS=
DISK=$1
MOUNTPATH=$2
PARENT=`dirname $MOUNTPATH`

retries=10
echo "=== do_unmount $MOUNTPATH ==="
while ! /bin/umount $MOUNTPATH; do
    s1=`stat -c %d $MOUNTPATH`
    s2=`stat -c %d $PARENT`
    if [[ $s1 = $s2 ]]; then
        echo "$MOUNTPATH already unmounted"
        exit 0
    fi
    echo "umount failed: $MOUNTPATH"
    ((retries--))
    if [ $retries -le 0 ]; then
        exit 1
    fi
    /bin/sleep 1
done
echo "unmount complete: $MOUNTPATH"
