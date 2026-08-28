#!/bin/bash
# ==================================================================
# FAST Core Array - Observation Driver
# Version: v0.2.7
# Updates: Duration m/h support, null-safe modes, SIGHUP, disk check
# ==================================================================

# --- CONFIGURATION ---
DRIVER_VERSION="v0.2.7"          # The version of THIS script
SUPPORTED_DATA_VERSION="v0.1.0"  # The JSON format version this script understands

JSON_FILE="${1:-${HOME}/schedule/schedule.json}"
WORKER_SCRIPT="${HOME}/scripts/run_data_recorder.sh"
TRIGGER_SCRIPT="${HOME}/observe/roach_trigger.py"
LOG_DIR="${HOME}/log"

WORKER_NODES=("a01" "a02")

declare -A ROACH_HOSTS
ROACH_HOSTS["CA01"]="r2170"
ROACH_HOSTS["CA02"]="r2171"
ROACH_HOSTS["CA03"]="r2172"
ROACH_HOSTS["CA04"]="r2173"

FPGA_CLK=250000000

# --- LOGGING ---
mkdir -p "$LOG_DIR"
SESSION_LOG="${LOG_DIR}/driver_session_$(date +%Y%m%d-%H%M%S).log"
echo "[DRIVER] Logging to: $SESSION_LOG"
exec > >(tee -a "$SESSION_LOG") 2>&1

# --- TERMINATION ---
cleanup() {
    echo ""
    echo "=========================================================="
    echo "[DRIVER] 🛑 ABORT SIGNAL RECEIVED"
    echo "=========================================================="
    pkill -f "roach_trigger.py" 2>/dev/null
    for host in "${WORKER_NODES[@]}"; do
        ssh -o ConnectTimeout=3 ${USER}@$host "pkill -f run_data_recorder.sh; killall -q -9 mbspec specrecv specrecv2 bbrec" 2>/dev/null
    done
    exit 1
}
trap cleanup SIGHUP SIGINT SIGTERM

# --- CHECKS ---
if ! command -v jq &> /dev/null; then echo "Error: 'jq' missing."; exit 1; fi
if [ ! -f "$JSON_FILE" ]; then echo "Error: $JSON_FILE missing."; exit 1; fi

# --- VERSION CHECK (Decoupled Logic) ---
json_ver=$(jq -r '.version // "null"' "$JSON_FILE")

get_mm_version() { echo "${1#v}" | awk -F. '{print $1"."$2}'; }

# Check if the JSON version matches what this driver supports
if [ "$(get_mm_version "$SUPPORTED_DATA_VERSION")" != "$(get_mm_version "$json_ver")" ]; then
    echo "---------------------------------------------------"
    echo "[CRITICAL ERROR] Data Format Mismatch!"
    echo "  > This driver supports JSON format: $SUPPORTED_DATA_VERSION"
    echo "  > The schedule file is version:     $json_ver"
    echo "  > Driver Script Version:            $DRIVER_VERSION"
    echo "---------------------------------------------------"
    exit 1
fi

task_count=$(jq '.schedule | length' "$JSON_FILE")
echo "Loaded $task_count tasks from $JSON_FILE (Data Format: $json_ver)"

# --- HELPER FUNCTIONS ---
set_acc_len() {
    local host=$1; local val=$2
    { echo "?wordwrite u0_acc_len 0 $val"; echo "?wordwrite u1_acc_len 0 $val"; } | nc -w 1 $host 7147 > /dev/null
}

set_noise_cal() {
    local host=$1; local on_sec=$2; local off_sec=$3
    local on_cnt=$(awk -v t="$on_sec" -v f="$FPGA_CLK" 'BEGIN { printf "%.0f", t * f }')
    local off_cnt=$(awk -v t="$off_sec" -v f="$FPGA_CLK" 'BEGIN { printf "%.0f", t * f }')
    {
        echo "?wordwrite noisecal_delay_hipart 0 0"; echo "?wordwrite noisecal_delay 0 0"
        echo "?wordwrite noisecal_on_hipart 0 $(( on_cnt >> 32 ))"; echo "?wordwrite noisecal_on 0 $(( on_cnt & 0xFFFFFFFF ))"
        echo "?wordwrite noisecal_off_hipart 0 $(( off_cnt >> 32 ))"; echo "?wordwrite noisecal_off 0 $(( off_cnt & 0xFFFFFFFF ))"
    } | nc -w 1 $host 7147 > /dev/null
}

# --- MAIN LOOP ---
for (( i=0; i<$task_count; i++ )); do
    # 1. READ JSON
    proj_id=$(jq -r ".schedule[$i].project_id" "$JSON_FILE")
    observer=$(jq -r ".schedule[$i].observer" "$JSON_FILE")
    src_name=$(jq -r ".schedule[$i].source" "$JSON_FILE")
    ra=$(jq -r ".schedule[$i].ra" "$JSON_FILE")
    dec=$(jq -r ".schedule[$i].dec" "$JSON_FILE")
    receiver=$(jq -r ".schedule[$i].receiver" "$JSON_FILE")
    obs_mode=$(jq -r ".schedule[$i].mode" "$JSON_FILE")
    start_time=$(jq -r ".schedule[$i].start_time_cst" "$JSON_FILE")
    # Duration may be given in s/m/h (GUI scheduler emits any of these)
    duration_str=$(jq -r ".schedule[$i].duration" "$JSON_FILE")
    case "$duration_str" in
        *m) duration=$(( ${duration_str%m} * 60 )) ;;
        *h) duration=$(( ${duration_str%h} * 3600 )) ;;
        *)  duration=${duration_str%s} ;;
    esac
    
    spec_en=$(jq -r ".schedule[$i].spec_enabled" "$JSON_FILE")
    spec_integ=$(jq -r ".schedule[$i].spec_integ" "$JSON_FILE")
    spec_mode=$(jq -r ".schedule[$i].spec_mode // \"F\"" "$JSON_FILE")
    psr_en=$(jq -r ".schedule[$i].psr_enabled" "$JSON_FILE")
    psr_integ=$(jq -r ".schedule[$i].psr_integ" "$JSON_FILE")
    psr_mode=$(jq -r ".schedule[$i].psr_mode // \"2-Pols\"" "$JSON_FILE")
    bb_en=$(jq -r ".schedule[$i].baseband_enabled" "$JSON_FILE")
    cal_on=$(jq -r ".schedule[$i].cal_on" "$JSON_FILE")
    cal_off=$(jq -r ".schedule[$i].cal_off" "$JSON_FILE")
    antennas=$(jq -r ".schedule[$i].antennas | join(\" \")" "$JSON_FILE")

    echo "---------------------------------------------------"
    echo "SYSTEM TIME: $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "Task $((i+1)): $src_name | Start: $start_time (CST)"

    # 2. PREPARE PARAMETERS
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

    # 3. ANTENNA MAPPING
    use_a01=false; use_a02=false; ants_a01=""; ants_a02=""; roach_list=""
    for ant in $antennas; do
        if [[ "$ant" == "CA01" ]] || [[ "$ant" == "CA02" ]]; then use_a01=true; ants_a01+="$ant "; fi
        if [[ "$ant" == "CA03" ]] || [[ "$ant" == "CA04" ]]; then use_a02=true; ants_a02+="$ant "; fi
        roach_list+="${ROACH_HOSTS[$ant]} "
    done

    # 3b. DISK CHECK on every node this task uses (skip task if >95%)
    if [ "$use_a01" = true ] && ! ssh -o ConnectTimeout=3 ${USER}@a01 "df --output=pcent /disk | tail -1 | tr -d ' %'" | grep -qxE '([0-9]|[1-8][0-9]|9[0-4])' 2>/dev/null; then
        echo "[CRITICAL] Disk full or unreachable on a01. Skipping task $((i+1))."
        continue
    fi
    if [ "$use_a02" = true ] && ! ssh -o ConnectTimeout=3 ${USER}@a02 "df --output=pcent /disk | tail -1 | tr -d ' %'" | grep -qxE '([0-9]|[1-8][0-9]|9[0-4])' 2>/dev/null; then
        echo "[CRITICAL] Disk full or unreachable on a02. Skipping task $((i+1))."
        continue
    fi

    # 4. TIMING
    target_unix=$(date -d "$start_time CST" +%s)
    current_unix=$(date +%s)
    remaining_sec=$(( target_unix - current_unix ))

    if [ $remaining_sec -lt 10 ]; then
        echo "[WARNING] Start time is too close or in past ($remaining_sec s). Skipping."
        continue
    fi

    echo "Configuring ROACH boards..."
    for ant in $antennas; do
        host=${ROACH_HOSTS[$ant]}
        if [ $roach_acc_val -gt 0 ]; then set_acc_len $host $roach_acc_val; fi
        set_noise_cal $host $cal_on $cal_off
    done
    
    wait_sec=$(( remaining_sec - 20 ))
    if [ $wait_sec -gt 0 ]; then echo "Waiting ${wait_sec}s..."; sleep $wait_sec; fi

    # 5. LAUNCH WORKERS
    backends_flag=""
    if [ "$spec_en" == "true" ]; then backends_flag+="Spec,"; fi
    if [ "$psr_en" == "true" ]; then backends_flag+="PSR,"; fi
    if [ "$bb_en" == "true" ]; then backends_flag+="Baseband,"; fi

    meta_args="-p \"$proj_id\" -o \"$observer\" -R \"$ra\" -D \"$dec\" -r \"$receiver\" -M \"$obs_mode\""

    echo "Launching Worker Scripts..."
    launch_worker() {
        local host=$1; local ants=$2
        
        # Base Command
        local cmd="$WORKER_SCRIPT -s \"$src_name\" -t \"$start_time\" -d $duration -a \"$ants\" -B \"$backends_flag\" $meta_args --psr_mode \"$psr_mode\" --spec_mode \"$spec_mode\""
        
        # Robust Append
        if [ -n "$spec_args" ]; then 
            cmd="$cmd --spec_params \"$spec_args\""
        fi
        
        if [ -n "$psr_args" ]; then 
            cmd="$cmd --psr_params \"$psr_args\""
        fi
        
        ssh -n ${USER}@$host "$cmd" &
    }

    if [ "$use_a01" = true ]; then launch_worker "a01" "$ants_a01"; pid_a01=$!; fi
    if [ "$use_a02" = true ]; then launch_worker "a02" "$ants_a02"; pid_a02=$!; fi

    echo "Waiting for T=0..."
    current_unix=$(date +%s)
    pre_wait=$(( target_unix - current_unix - 5 ))
    if [ $pre_wait -gt 0 ]; then sleep $pre_wait; fi
    
    $TRIGGER_SCRIPT -t "$start_time" -r $roach_list

    if [ "$use_a01" = true ]; then wait $pid_a01; fi
    if [ "$use_a02" = true ]; then wait $pid_a02; fi
    echo "Task Complete."
done
