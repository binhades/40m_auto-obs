#!/usr/bin/env bash

#scp obs_scheduler.py  bliu@wiki.fast:/home/bliu/observe/
#scp obs_utils.py      bliu@wiki.fast:/home/bliu/observe/

scp run_auto_obs.sh   obs@atlas:/home/obs/observe/
scp run_auto_obs.py   obs@atlas:/home/obs/observe/
#
scp roach_trigger.py  obs@atlas:/home/obs/observe/
#
scp run_data_recorder.sh obs@a01:/home/obs/scripts/run_data_recorder.sh
scp run_data_recorder.sh obs@a02:/home/obs/scripts/run_data_recorder.sh
scp run_data_recorder.sh bliu@a01:/home/bliu/scripts/run_data_recorder.sh
scp run_data_recorder.sh bliu@a02:/home/bliu/scripts/run_data_recorder.sh
#
scp obs_controller.py obs@atlas:/home/obs/observe/
scp obs_scheduler.py  obs@atlas:/home/obs/observe/
scp obs_uploader.py   obs@atlas:/home/obs/observe/
scp obs_utils.py      obs@atlas:/home/obs/observe/
scp roach_tools.py    obs@atlas:/home/obs/observe/


