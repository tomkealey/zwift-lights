#!/bin/bash
# Re-initialise the Raspberry Pi 3's Bluetooth chip if it failed to start at boot.
# The Pi 3's chip sometimes times out during its startup (often after a voltage
# dip at power-on), leaving no usable controller. Resetting its driver fixes it.
# Installed by install.sh as /usr/local/sbin/bt-recover.sh, run once per boot.
for i in 1 2 3; do
  sleep 10
  if bluetoothctl show 2>/dev/null | grep -q "Powered: yes"; then exit 0; fi
  if bluetoothctl show 2>/dev/null | grep -q "Controller"; then
    bluetoothctl power on && exit 0
  fi
  logger -t bt-recover "no Bluetooth controller, resetting the chip (attempt $i)"
  echo serial0-0 > /sys/bus/serial/drivers/hci_uart_bcm/unbind 2>/dev/null
  sleep 2
  echo serial0-0 > /sys/bus/serial/drivers/hci_uart_bcm/bind 2>/dev/null
  sleep 5
  systemctl restart bluetooth
done
logger -t bt-recover "gave up after 3 attempts"
exit 1
