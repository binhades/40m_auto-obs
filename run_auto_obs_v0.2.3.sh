#!/bin/bash
# ==================================================================
# FAST Core Array - Observation Driver
# Version: v0.2.3
# Updates: Timestamping, Tee-Logging, Variable Safety
# ==================================================================

# --- CONFIGURATION ---
DRIVER_VERSION="v0.1.0"
JSON_FILE="${1:-${HOME}/schedule/schedule.json}"
WORKER_SCRIPT="${HOME}/scripts/run_data_recorder.sh"
TRIGGER_SCRIPT="${HOME}/observe/roach_trigger.py"
LOG_DIR="${HOME}/log"

# Define Worker Nodes Explicitly for Cleanup
WORKER_NODES=("a01" "a02")

declare -A ROACH_HOSTS
ROACH_HOSTS["CA01"]="r2170"
ROACH_HOSTS["CA02"]="r2171"
ROACH_HOSTS["CA03"]="r2172"
ROACH_HOSTS["CA04"]="r2173"

FPGA_CLK=250000000

# --- 1. LOGGING SETUP (Non-Blocking) ---
mkdir -p "$LOG_DIR"
# Generate a unique session log name
SESSION_LOG="${LOG_DIR}/driver_session_$(date +%Y%m%d-%H%M%S).log"
echo "[DRIVER] Logging this session to: $SESSION_LOG"

# Redirect ALL output (stdout & stderr) to 'tee'.
# 'tee' writes to the file AND passes it back to stdout (for the Controller).
exec > >(tee -a "$SESSION_LOG") 2>&1

# --- TERMINATION LOGIC ---
cleanup() {
    echo ""
    echo "=========================================================="
    echo "[DRIVER] 🛑 ABORT SIGNAL RECEIVED (Ctrl+C)"
    echo "=========================================================="
    
    # 1. Kill Local Trigger if running
    echo "[DRIVER] Killing local trigger..."
    pkill -f "roach_trigger.py" 2>/dev/null

    # 2. Kill Remote Workers (Aggressive)
    echo "[DRIVER] Terminating remote processes on workers..."
    
    for host in "${WORKER_NODES[@]}"; do
        echo "  -> connecting to $host..."
        # We use 'ssh -o ConnectTimeout=3' to prevent hanging if a node is down
        ssh -o ConnectTimeout=3 ${USER}@$host "pkill -f run_data_recorder.sh; \
            killall -q -9 mbspec specrecv specrecv2 bbrec" 2>/dev/null
    done

    echo "[DRIVER] 🧹 Cleanup Complete. Exiting."
    exit 1
}

# Trap SIGINT (Ctrl+C) and SIGTERM
trap cleanup SIGINT SIGTERM

# --- PRE-FLIGHT CHECKS ---
if ! command -v jq &> /dev/null; then echo "Error: 'jq' missing."; exit 1; fi
if [ ! -f "$JSON_FILE" ]; then echo "Error: $JSON_FILE missing."; exit 1; fi

# --- VERSION CHECK ---
json_ver=$(jq -r '.version // "null"' "$JSON_FILE")
if [ "$json_ver" == "null" ]; then echo "[ERROR] JSON missing 'version'."; exit 1; fi

get_mm_version() { echo "${1#v}" | awk -F. '{print $1"."$2}'; }
driver_mm=$(get_mm_version "$DRIVER_VERSION")
json_mm=$(get_mm_version "$json_ver")

if [ "$driver_mm" != "$json_mm" ]; then
    echo "[CRITICAL ERROR] Version Incompatibility! Driver: $DRIVER_VERSION, JSON: $json_ver"
    exit 1
fi

# --- LOAD SCHEDULE ---
task_count=$(jq '.schedule | length' "$JSON_FILE")
echo "Loaded $task_count tasks from $JSON_FILE"

# --- FUNCTIONS ---
set_acc_len() {
    local host=$1; local val=$2
    # Check if host is reachable first? No, nc -w 1 handles timeout.
    { echo "?wordwrite u0_acc_len 0 $val"; echo "?wordwrite u1_acc_len 0 $val"; } | nc -w 1 $host 7147 > /dev/null
}

set_noise_cal() {
    local host=$1; local on_sec=$2; local off_sec=$3
    local on_cnt=$(awk -v t="$on_sec" -v f="$FPGA_CLK" 'BEGIN { printf "%.0f", t * f }')
    local off_cnt=$(awk -v t="$off_sec" -v f="$FPGA_CLK" 'BEGIN { printf "%.0f", t * f }')
    {
        echo "?wordwrite noisecal_delay_hipart 0 0"
        echo "?wordwrite noisecal_delay 0 0"
        echo "?wordwrite noisecal_on_hipart 0 $(( on_cnt >> 32 ))"
        echo "?wordwrite noisecal_on 0 $(( on_cnt & 0xFFFFFFFF ))"
        echo "?wordwrite noisecal_off_hipart 0 $(( off_cnt >> 32 ))"
        echo "?wordwrite noisecal_off 0 $(( off_cnt & 0xFFFFFFFF ))"
    } | nc -w 1 $host 7147 > /dev/null
}

# --- MAIN LOOP ---
for (( i=0; i<$task_count; i++ )); do

    # 1. Metadata
    proj_id=$(jq -r ".schedule[$i].project_id" "$JSON_FILE")
    observer=$(jq -r ".schedule[$i].observer" "$JSON_FILE")
    src_name=$(jq -r ".schedule[$i].source" "$JSON_FILE")
    ra=$(jq -r ".schedule[$i].ra" "$JSON_FILE")
    dec=$(jq -r ".schedule[$i].dec" "$JSON_FILE")
    receiver=$(jq -r ".schedule[$i].receiver" "$JSON_FILE")
    obs_mode=$(jq -r ".schedule[$i].mode" "$JSON_FILE")
    
    start_time=$(jq -r ".schedule[$i].start_time_cst" "$JSON_FILE")
    duration_str=$(jq -r ".schedule[$i].duration" "$JSON_FILE"); duration=${duration_str%s}
    
    # 2. Config
    spec_en=$(jq -r ".schedule[$i].spec_enabled" "$JSON_FILE")
    spec_integ=$(jq -r ".schedule[$i].spec_integ" "$JSON_FILE")
    spec_mode=$(jq -r ".schedule[$i].spec_mode // \"F\"" "$JSON_FILE")
    
    psr_en=$(jq -r ".schedule[$i].psr_enabled" "$JSON_FILE")
    psr_integ=$(jq -r ".schedule[$i].psr_integ" "$JSON_FILE")
    psr_mode=$(jq -r ".schedule[$i].psr_mode" "$JSON_FILE")
    bb_en=$(jq -r ".schedule[$i].baseband_enabled" "$JSON_FILE")
    
    cal_on=$(jq -r ".schedule[$i].cal_on" "$JSON_FILE")
    cal_off=$(jq -r ".schedule[$i].cal_off" "$JSON_FILE")
    antennas=$(jq -r ".schedule[$i].antennas | join(\" \")" "$JSON_FILE")

    echo "---------------------------------------------------"
    # NEW: Print System Time
    echo "SYSTEM TIME: $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "Task $((i+1)): $src_name | Start: $start_time (CST)"

    # Hardware Params
    spec_args=""; psr_args=""; roach_acc_val=5

    if [ "$spec_en" == "true" ]; then
        case "$spec_integ" in
            "0.1s") spec_args="4 20 48 48" ;;
            "1.0s") spec_args="4 20 480 80" ;;
            *) echo "Error: Unknown Spec Integ"; exit 1 ;;
        esac
    fi

    if [ "$psr_en" == "true" ]; then
        case "$psr_integ" in
            "50us")  psr_acc=5 ;;
            "100us") psr_acc=11 ;;
            "200us") psr_acc=23 ;;
            *) echo "Error: Unknown PSR Integ"; exit 1 ;;
        esac
        psr_args="$psr_acc"
        roach_acc_val=$psr_acc
    fi

    use_a01=false; use_a02=false; ants_a01=""; ants_a02=""; roach_list=""

    for ant in $antennas; do
        if [[ "$ant" == "CA01" ]] || [[ "$ant" == "CA02" ]]; then use_a01=true; ants_a01+="$ant "; fi
        if [[ "$ant" == "CA03" ]] || [[ "$ant" == "CA04" ]]; then use_a02=true; ants_a02+="$ant "; fi
        roach_list+="${ROACH_HOSTS[$ant]} "
    done

    # Timing Check (10s)
    target_unix=$(date -d "$start_time CST" +%s)
    current_unix=$(date +%s)
    remaining_sec=$(( target_unix - current_unix ))

    # If remaining_sec is negative (e.g. -600), we are late.
    if [ $remaining_sec -lt 10 ]; then
        echo "[WARNING] Start time is too close or in past ($remaining_sec s). Skipping."
        continue
    fi

    # Configure ROACH
    echo "Configuring ROACH boards..."
    for ant in $antennas; do
        host=${ROACH_HOSTS[$ant]}
        if [ $roach_acc_val -gt 0 ]; then set_acc_len $host $roach_acc_val; fi
        set_noise_cal $host $cal_on $cal_off
    done
    
    # Pre-Wait
    wait_sec=$(( remaining_sec - 20 ))
    if [ $wait_sec -gt 0 ]; then echo "Waiting ${wait_sec}s..."; sleep $wait_sec; fi

    # Launch Workers
    backends_flag=""
    if [ "$spec_en" == "true" ]; then backends_flag+="Spec,"; fi
    if [ "$psr_en" == "true" ]; then backends_flag+="PSR,"; fi
    if [ "$bb_en" == "true" ]; then backends_flag+="Baseband,"; fi

    meta_args="-p \"$proj_id\" -o \"$observer\" -R \"$ra\" -D \"$dec\" -r \"$receiver\" -M \"$obs_mode\""

    echo "Launching Worker Scripts..."
    launch_worker() {
        local host=$1; local ants=$2
        # Use src_name instead of source
        local cmd="$WORKER_SCRIPT -s \"$src_name\" -t \"$start_time\" -d $duration -a \"$ants\" -B \"$backends_flag\" $meta_args --psr_mode \"$psr_mode\" --spec_mode \"$spec_mode\""
        
        if [ ! -z "$spec_args" ]; then cmd+=\" --spec_params \\\"$spec_args\\\"\"; fi
        if [ ! -z \"$psr_args\" ]; then cmd+=\" --psr_params \\\"$psr_args\\\"\"; fi
        
        ssh ${USER}@$host "$cmd" &
    }

    if [ "$use_a01" = true ]; then launch_worker "a01" "$ants_a01"; pid_a01=$!; fi
    if [ "$use_a02" = true ]; then launch_worker "a02" "$ants_a02"; pid_a02=$!; fi

    # Trigger Handoff
    echo "Waiting for T=0..."
    current_unix=$(date +%s)
    pre_wait=$(( target_unix - current_unix - 5 ))
    if [ $pre_wait -gt 0 ]; then sleep $pre_wait; fi
    
    $TRIGGER_SCRIPT -t "$start_time" -r $roach_list

    if [ "$use_a01" = true ]; then wait $pid_a01; fi
    if [ "$use_a02" = true ]; then wait $pid_a02; fi
    echo "Task Complete."
done
