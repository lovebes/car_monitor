#!/bin/sh

IFS=

DISK=$1
MOUNTPATH=$2
RW=$3

echo "=== do_mount $MOUNTPATH ==="
mount $DISK $MOUNTPATH -t vfat
dpath=$MOUNTPATH/DCIM/MOVIE
if [ -d "$dpath" ]; then
    cd "$dpath"
    if [ -d RO ]; then
        echo "$MOUNTPATH: cleaning out RO/"
        chmod u+w RO/*
        mv RO/* .
        rmdir RO
    fi
    echo "$MOUNTPATH: cleaning small files"
    find -name \*.MP4 -size -4M -print -delete
    cd /
fi

if [ "$RW" != 1 ]; then
    echo "$MOUNTPATH: remounting read-only"
    mount $MOUNTPATH -o remount,ro
fi

echo "mount complete: $MOUNTPATH"
