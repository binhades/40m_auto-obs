#!/bin/bash
# ==================================================================
# FAST Core Array - Data Recorder
# Version: v0.1.1
# Updates: Log to 'active_${type}.log'
# ==================================================================

# --- TERMINATION TRAP ---
# If this script is killed (SIGINT/SIGTERM) or exits, kill all background child processes.
cleanup() {
    echo "[WORKER] Catching signal... cleaning up children."
    # Kill all child PIDs (jobs) associated with this shell
    kill $(jobs -p) 2>/dev/null
}
trap cleanup SIGINT SIGTERM EXIT

DATA_ROOT="${DATA0:-/disk}"
LOG_ROOT="${HOME}/log"
MY_HOSTNAME=$(hostname)
SRC_ROOT="${HOME}/scripts"

# Switch to script directory
cd "${SRC_ROOT}" || { echo "Error: Could not cd to ${SRC_ROOT}"; exit 1; }
mkdir -p "$LOG_ROOT"

if [[ "$MY_HOSTNAME" == "a01" ]]; then LOCAL_RECV_IP="192.168.70.111"
elif [[ "$MY_HOSTNAME" == "a02" ]]; then LOCAL_RECV_IP="192.168.70.121"
else LOCAL_RECV_IP="127.0.0.1"; fi

# --- ARGUMENTS ---
spec_en=false; psr_en=false; bb_en=false
psr_mode="2-Pols"
spec_mode="F" # Default

while getopts s:t:d:a:B:p:o:R:D:r:M:-: flag; do
    case "${flag}" in
        s) source_name=${OPTARG};;
        t) timestamp=${OPTARG};;
        d) duration=${OPTARG};;
        a) antennas=${OPTARG};;
        B) backends=${OPTARG};;
        p) proj_id=${OPTARG};;
        o) observer=${OPTARG};;
        R) ra=${OPTARG};;
        D) dec=${OPTARG};;
        r) receiver=${OPTARG};;
        M) obs_mode=${OPTARG};;
        -) 
            case "${OPTARG}" in
                spec_params) spec_params="${!OPTIND}"; OPTIND=$(( $OPTIND + 1 ));;
                psr_params) psr_params="${!OPTIND}"; OPTIND=$(( $OPTIND + 1 ));;
                psr_mode) psr_mode="${!OPTIND}"; OPTIND=$(( $OPTIND + 1 ));;
                spec_mode) spec_mode="${!OPTIND}"; OPTIND=$(( $OPTIND + 1 ));;
            esac;;
    esac
done

duration=${duration%s}
if [[ "$backends" == *"Spec"* ]]; then spec_en=true; fi
if [[ "$backends" == *"PSR"* ]]; then psr_en=true; fi
if [[ "$backends" == *"Baseband"* ]]; then bb_en=true; fi

# --- 3. LOG SETUP (CRITICAL NEW STEP) ---
# We create 'active' logs that the Controller will watch.
# We also back up the old logs for history.
#
setup_log() {
    local type=$1
    local active_file="${LOG_ROOT}/active_${type}.log"
    local history_file="${LOG_ROOT}/$(date +%Y%m%d-%H%M%S)_${source_name}_${type}.log"
    
    # 1. CRITICAL: Break the link to the previous run's data
    # This removes the name "active_spec.log" but leaves the data on disk
    # because "previous_history.log" is still holding onto it.
    rm -f "$active_file"

    # 2. Create a NEW file (New Inode)
    # This wipes the *new* file (which is empty anyway), not the old one.
    echo "--- STARTING ${type^^} BACKEND ---" > "$active_file"
    echo "Source: $source_name | Mode: $obs_mode" >> "$active_file"
    
    # 3. Create a fresh link for *this* run
    ln -f "$active_file" "$history_file"
}


if [ "$spec_en" = true ]; then setup_log "spec"; fi
if [ "$psr_en" = true ]; then setup_log "psr"; fi
if [ "$bb_en" = true ]; then setup_log "bb"; fi
# --- MAPPING & TIME ---
case "$obs_mode" in
    "Tracking")   mode_short="TRK" ;;
    "Drift-Scan") mode_short="DFT" ;;
    "Cross-Scan") mode_short="CRS" ;;
    "OTF")        mode_short="OTF" ;;
    "Position")   mode_short="POS" ;;
    "Test")       mode_short="TST" ;;
    *)            mode_short="UNK" ;;
esac

timestamp_utc=$(date -d "$timestamp CST" -u +%Y%m%dT%H%M%SZ)

day_dir=$(date -d "$timestamp" +%Y%m%d)
save_dir="${DATA_ROOT}/${proj_id}/${day_dir}"
mkdir -p "$save_dir"

echo "[WORKER $MY_HOSTNAME] $source_name ($duration s) | Mode: $mode_short | UTC: $timestamp_utc"
echo "[WORKER] Spec Mode: $spec_mode"

declare -A MC_MAP
MC_MAP["CA01"]="239.3.70.1:239.3.70.2:239.3.70.3:239.3.70.4"
MC_MAP["CA02"]="239.3.71.1:239.3.71.2:239.3.71.3:239.3.71.4"
MC_MAP["CA03"]="239.3.72.1:239.3.72.2:239.3.72.3:239.3.72.4"
MC_MAP["CA04"]="239.3.73.1:239.3.73.2:239.3.73.3:239.3.73.4"

# --- EXECUTION FUNCTIONS ---

run_mbspec() {
    local ant=$1
    local core=$2
    read ntaps log2nchan nacc nbatch <<< "$spec_params"
    
    # --- SPEC MODE LOGIC ---
    local nchan_wb=0
    local nchan_nb=0
    local nb_offs=0
    local gpuid=0
    
    # Hardware Configuration
    if [[ "$spec_mode" == *"F"* ]]; then
        # Full Mode (Original Defaults)
        nchan_wb=1048576
        nchan_nb=0
        nb_offs=0
    else
        # W and/or N Mode
        if [[ "$spec_mode" == *"W"* ]]; then
            nchan_wb=65536
        fi
        
        if [[ "$spec_mode" == *"N"* ]]; then
            nchan_nb=65536
            # Calc: (1420 - 1000) * 2^20 / 500 - 32768 = 848035
            nb_offs=848035
        fi
    fi
    
    # Rec Rows Calculation
    # CRITICAL FIX: Fixed to 1048576 regardless of mode
    local nchan_calc=1048576
    
    local bandwidth_hz=500000000
    local seconds=$(( duration + 1 ))
    local rec_rows=$(( (seconds * bandwidth_hz) / nacc / nchan_calc ))
    
    echo "[WORKER] Spec Setup: WB=$nchan_wb, NB=$nchan_nb, Offs=$nb_offs, Rows=$rec_rows"

    local basename="${save_dir}/${source_name}_${mode_short}_${ant}_S_${timestamp_utc}"
    local log_file="${LOG_ROOT}/$(date +%Y%m%d-%H%M%S).${ant}.spec.log"
    
    IFS=':' read px py sx sy <<< "${MC_MAP[$ant]}"
    
    echo "[WORKER] Launching mbspec for $ant on Cores $core & $((core+1))..."
    
    ./mbspec $ntaps $log2nchan $nacc $nbatch $gpuid \
        $nchan_wb $nchan_nb $nb_offs $rec_rows "$basename" \
        "${LOCAL_RECV_IP}@$((core+0))" "${sx}:12345" \
        "${LOCAL_RECV_IP}@$((core+1))" "${sy}:12345" \
        >> "${LOG_ROOT}/active_spec.log" 2>&1 &
        
    pids+=($!)
}

run_specrecv() {
    local ant=$1; local core=$2; local psr_acc=$psr_params
    local bandwidth=500000000; local num_fft=4096; local nsblk=1024
    local nacc=$(( psr_acc + 1 ))
    local calc_duration=$(( duration + 1 ))
    local total_subints=$(( calc_duration * bandwidth / nacc / num_fft / nsblk ))

    local fname="${save_dir}/${source_name}_${mode_short}_${ant}_P_${timestamp_utc}"
    local log_file="${LOG_ROOT}/$(date +%Y%m%d-%H%M%S).${ant}.psr.log"
    
    IFS=':' read px py sx sy <<< "${MC_MAP[$ant]}"
    
    if [ "$psr_mode" == "Stokes" ]; then
        echo "[WORKER] Launching specrecv2 (Stokes) for $ant..."
    	local fits_args="${observer},${source_name},${proj_id},${ra},${dec},FAST_CA,${receiver},MB4k,LIN,1250,500"
        ./specrecv2 "${LOCAL_RECV_IP}@${core}" "${px}:12345" \
            "${LOCAL_RECV_IP}@$((core+1))" "${py}:12345" \
            "$fname" 4096 $psr_acc 1 0-4095 1024 8192 $total_subints 1 1 "$fits_args" \
            >> "${LOG_ROOT}/active_psr.log" 2>&1 &
            2>&1 | tee "$log_file" &
    else
        echo "[WORKER] Launching specrecv (2-Pols) for $ant..."
    	local fits_args="${observer},${source_name},${proj_id},${ra},${dec},FAST_CA,${receiver},LIN,1250,500"
        ./specrecv "${LOCAL_RECV_IP}@${core}" "${px}:12345" \
            "$fname" 4096 $psr_acc 1 0-4095 1024 8192 $total_subints 1 1 "$fits_args" \
            >> "${LOG_ROOT}/active_psr.log" 2>&1 &
     fi
    pids+=($!)
}

run_bbrec() {
    local ant=$1; local core=$2
    local total_size_gb=$(echo "2 * $duration" | bc -l)
    local gb_to_gib=0.93132348
    local total_size_gib=$(echo "$total_size_gb * $gb_to_gib" | bc -l)
    total_size_gib=$(printf "%.0f" "$total_size_gib")
    
    local basename="${save_dir}/${source_name}_${mode_short}_${ant}_B"
    local log_file="${LOG_ROOT}/$(date +%Y%m%d-%H%M%S).${ant}.bb.log"
    
    IFS=':' read px py sx sy <<< "${MC_MAP[$ant]}"
    
    echo "[WORKER] Launching bbrec for $ant on Core $core..."
    ./bbrec -t --direct-io --file-size 64G --total-size ${total_size_gib} \
        "$basename" \
        "${LOCAL_RECV_IP}@${core}" "${sx}:12345" \
        "${LOCAL_RECV_IP}@$((core+1))" "${sy}:12345" \
        >> "${LOG_ROOT}/active_bb.log" 2>&1 &
    pids+=($!)
}

# --- MAIN LOOP ---
pids=()
ant_idx=0
for ant in $antennas; do
    base_cpu=$(( 160 + (ant_idx * 10) ))
    if [ "$spec_en" = true ]; then run_mbspec "$ant" "$((base_cpu + 0))"; fi
    if [ "$psr_en" = true ]; then run_specrecv "$ant" "$((base_cpu + 2))"; fi
    if [ "$bb_en" = true ]; then run_bbrec "$ant" "$((base_cpu + 4))"; fi
    ((ant_idx++))
done

for pid in "${pids[@]}"; do wait $pid; done
echo "[WORKER] Finished."
