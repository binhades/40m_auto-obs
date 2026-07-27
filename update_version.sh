#!/usr/bin/env bash

#-----------------------------------------
#-----------------------------------------
#ver="v0.1.0" # work good.
#ver="v0.2.3" # error
#ver="v0.2.4" # 
ver="v0.2.6" # update version control 
cp run_auto_obs_${ver}.sh run_auto_obs.sh
#-----------------------------------------
#-----------------------------------------
#ver="v0.1.0"
#ver="v0.1.1"
#ver="v0.1.2"
#ver="v0.1.6"
ver="v0.1.7" # baseband to new folder
cp run_data_recorder_${ver}.sh run_data_recorder.sh
#-----------------------------------------
#-----------------------------------------
ver="v0.1.0"
cp roach_trigger_${ver}.py roach_trigger.py
#-----------------------------------------
#-----------------------------------------
#ver="v0.1.1"
ver="v0.1.2"
cp roach_tools_${ver}.py roach_tools.py
#-----------------------------------------
#-----------------------------------------
#ver="v0.1.1" # update version control 
#ver="v0.1.2" # The Production-Ready
ver="v0.1.5" # new roach tools
cp run_auto_obs_${ver}.py run_auto_obs.py
#-----------------------------------------
#-----------------------------------------
#ver="v0.1.0"
#ver="v0.2.1" # run_auto_obs.sh
#ver="v0.3.1" # start to use run_auto_obs.py 
#ver="v0.3.2"
#ver="v0.3.3"
#ver="v0.3.4"
#ver="v0.3.5"
ver="v0.3.6"
cp obs_controller_${ver}.py obs_controller.py
#-----------------------------------------
#-----------------------------------------
#ver="v0.1.0"
#ver="v0.3.0" # update version control
#ver="v0.3.2" # use obs_utils 
ver="v0.3.3" # work with new obs_utils_v0.3.9
cp obs_scheduler_${ver}.py obs_scheduler.py
#-----------------------------------------
#-----------------------------------------
#ver="v0.1.1"
#ver="v0.1.2"
#ver="v0.2.0"
#ver="v0.2.1"
#ver="v0.3.1"
#ver="v0.3.3"
#ver="v0.3.5" 
#ver="v0.3.6" 
#ver="v0.3.7" 
#ver="v0.3.8" 
#ver="v0.3.9" 
#ver="v0.3.10" 
ver="v0.3.11" 
cp obs_utils_${ver}.py obs_utils.py
#-----------------------------------------
#-----------------------------------------
#ver="v0.1.0" # errors
#ver="v0.1.1"
#ver="v0.1.2"
#ver="v0.1.3"
#ver="v0.1.9"
#ver="v0.2.0"
#ver="v0.2.1"
#ver="v0.2.2"
#ver="v0.2.3"
#ver="v0.2.5"
#ver="v0.2.6"
#ver="v0.2.7"
ver="v0.2.8"
cp obs_uploader_${ver}.py obs_uploader.py
#-----------------------------------------
#-----------------------------------------

chmod +x run_auto_obs.sh
chmod +x run_data_recorder.sh
chmod +x roach_trigger.py

chmod +x run_auto_obs.py
chmod +x obs_scheduler.py
chmod +x obs_uploader.py
chmod +x obs_controller.py
#-----------------------------------------
#-----------------------------------------

