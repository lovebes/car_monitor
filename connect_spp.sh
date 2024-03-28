#!/bin/sh

# Bluetooth MAC address of phone
addr=`jq --raw-output .phone_addr < config.json`

channel=$(sdptool search --bdaddr $addr SP | awk '/Channel:/ { print $2 }')
echo "channel = $channel"
if [ -n "$channel" ]; then
    exec stdbuf -oL rfcomm connect rfcomm0 $addr $channel
else
    sleep 4
fi
