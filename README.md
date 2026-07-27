# FAST 40m Telescope Observation Control System

Automatic observation control system for the FAST core array 40m telescope.

## Components

### Core Modules

- **obs_controller.py** - Main observation controller
- **obs_scheduler.py** - Observation scheduling and planning
- **obs_uploader.py** - Data upload management
- **obs_utils.py** - Common utilities and helper functions
- **roach_tools.py** - ROACH board interface tools
- **roach_trigger.py** - Trigger management for ROACH

### Run Scripts

- **run_auto_obs.py** - Main entry point for automated observations
- **run_auto_obs.sh** - Shell wrapper for auto observation
- **run_data_recorder.sh** - Data recording service launcher
- **update_version.sh** - Version management utility
- **sync_scripts.sh** - Script synchronization tool

### Templates

- **FAST_sdfits_template.txt** - SDFITS format template
- **PSRFITS_v3.4_search_template.txt** - PSRFITS search mode template

### Service Configuration

- **service_*.readme** - System service setup instructions

## Usage

```bash
# Start automatic observation
./run_auto_obs.sh

# Start data recorder
./run_data_recorder.sh
```

## Development

This project uses Git for version control. All changes are tracked through commits rather than versioned filenames.

## History

Migrated from manual version-numbered files (v0.x.x) to Git-based version control on 2026-07-27.
