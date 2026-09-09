#!/usr/bin/env bash
#
#
# restart 

sudo systemctl restart fast-obs-ctrl
sudo systemctl restart fast-obs-sche
sudo systemctl restart fast-obs-load

sleep 1

sudo systemctl status fast-obs-ctrl
sudo systemctl status fast-obs-sche
sudo systemctl status fast-obs-load
