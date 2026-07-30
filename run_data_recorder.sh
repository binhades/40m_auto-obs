#!/bin/bash
# ==================================================================
# FAST Core Array - Data Recorder
# Version: v0.1.6
# Updates: Baseband Data to different Folder
# ==================================================================

# --- TERMINATION TRAP ---
cleanup() {
    echo "[WORKER] Catching signal... cleaning up children."
    kill $(jobs -p) 2>/dev/null
}
trap cleanup SIGINT SIGTERM EXIT

DATA_ROOT="${DATA0:-/disk}"
LOG_ROOT="${HOME}/log"
MY_HOSTNAME=$(hostname)
SRC_ROOT="${HOME}/scripts"

cd "${SRC_ROOT}" || { echo "Error: Could not cd to ${SRC_ROOT}"; exit 1; }
mkdir -p "$LOG_ROOT"

if [[ "$MY_HOSTNAME" == "a01" ]]; then LOCAL_RECV_IP="192.168.70.111"
elif [[ "$MY_HOSTNAME" == "a02" ]]; then LOCAL_RECV_IP="192.168.70.121"
else LOCAL_RECV_IP="127.0.0.1"; fi

# --- ARGUMENTS ---
spec_en=false; psr_en=false; bb_en=false
psr_mode="2-Pols"
spec_mode="F"

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

# --- 3. LOG SETUP ---
setup_log() {
    local ant=$1
    local type=$2
    local active_file="${LOG_ROOT}/active_${type}_${ant}.log"
    local history_file="${LOG_ROOT}/$(date +%Y%m%d-%H%M%S)_${source_name}_${type}_${ant}.log"
    
    rm -f "$active_file"
    echo "--- STARTING ${type^^} BACKEND FOR ${ant} ---" > "$active_file"
    echo "Source: $source_name | Mode: $obs_mode" >> "$active_file"
    ln -f "$active_file" "$history_file"
}

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


if [ "$bb_en" = true ]; then
    save_dir="${DATA_ROOT}/baseband_40m/${proj_id}/${source_name}/${day_dir}"
else
    save_dir="${DATA_ROOT}/${proj_id}/${source_name}/${day_dir}"
fi
mkdir -p "$save_dir"

echo "[WORKER $MY_HOSTNAME] $source_name ($duration s) | Mode: $mode_short | UTC: $timestamp_utc"

declare -A MC_MAP
MC_MAP["CA01"]="239.3.70.1:239.3.70.2:239.3.70.3:239.3.70.4"
MC_MAP["CA02"]="239.3.71.1:239.3.71.2:239.3.71.3:239.3.71.4"
MC_MAP["CA03"]="239.3.72.1:239.3.72.2:239.3.72.3:239.3.72.4"
MC_MAP["CA04"]="239.3.73.1:239.3.73.2:239.3.73.3:239.3.73.4"

# --- EXECUTION FUNCTIONS ---

run_mbspec() {
    local ant=$1
    local core=$2
    local gpu=0  # <--- FORCED SINGLE GPU (Shared)
    
    if [ -z "$spec_params" ]; then
        echo "[ERROR] mbspec failed: --spec_params missing!" >> "${LOG_ROOT}/active_spec_${ant}.log"
        return 1
    fi
    
    read ntaps log2nchan nacc nbatch <<< "$spec_params"
    
    local nchan_wb=0; local nchan_nb=0; local nb_offs=0
    if [[ "$spec_mode" == *"F"* ]]; then
        nchan_wb=1048576; nchan_nb=0; nb_offs=0
    else
        if [[ "$spec_mode" == *"W"* ]]; then nchan_wb=65536; fi
        if [[ "$spec_mode" == *"N"* ]]; then nchan_nb=65536; nb_offs=848035; fi
    fi
    
    local nchan_calc=1048576
    local bandwidth_hz=500000000
    local seconds=$(( duration + 1 ))
    local rec_rows=$(( (seconds * bandwidth_hz) / nacc / nchan_calc ))
    
    local basename="${save_dir}/${source_name}_${mode_short}_${ant}_S_${timestamp_utc}"
    local active_log="${LOG_ROOT}/active_spec_${ant}.log"
    
    IFS=':' read px py sx sy <<< "${MC_MAP[$ant]}"
    
    # --- TARGETED PRE-FLIGHT CLEANUP ---
    # Only kill mbspec processes related to THIS antenna
    pkill -f "mbspec .*_${ant}_" 
    
    echo "[WORKER] Launching mbspec for $ant (CPU: $core, GPU: $gpu)..." >> "$active_log"
    
    ./mbspec $ntaps $log2nchan $nacc $nbatch $gpu \
        $nchan_wb $nchan_nb $nb_offs $rec_rows "$basename" \
        "${LOCAL_RECV_IP}@$((core+0))" "${sx}:12345" \
        "${LOCAL_RECV_IP}@$((core+1))" "${sy}:12345" \
        >> "$active_log" 2>&1 &
        
    pids+=($!)
}

run_specrecv() {
    local ant=$1; local core=$2; local psr_acc=$psr_params
    local bandwidth=500000000; local num_fft=4096; local nsblk=1024
    local nacc=$(( psr_acc + 1 ))
    local calc_duration=$(( duration + 1 ))
    local total_subints=$(( calc_duration * bandwidth / nacc / num_fft / nsblk ))

    local fname="${save_dir}/${source_name}_${mode_short}_${ant}_P_${timestamp_utc}"
    local active_log="${LOG_ROOT}/active_psr_${ant}.log"
    
    IFS=':' read px py sx sy <<< "${MC_MAP[$ant]}"
    
    # Cleanup only this antenna
    pkill -f "specrecv .*_${ant}_"
    
    echo "[WORKER] Launching specrecv for $ant (CPU: $core)..." >> "$active_log"

    if [ "$psr_mode" == "Stokes" ]; then
    	local fits_args="${observer},${source_name},${proj_id},${ra},${dec},FAST_CA,${receiver},MB4k,LIN,1250,500"
        ./specrecv2 "${LOCAL_RECV_IP}@${core}" "${px}:12345" \
            "${LOCAL_RECV_IP}@$((core+1))" "${py}:12345" \
            "$fname" 4096 $psr_acc 1 0-4095 1024 8192 $total_subints 1 1 "$fits_args" \
            >> "$active_log" 2>&1 &
    else
    	local fits_args="${observer},${source_name},${proj_id},${ra},${dec},FAST_CA,${receiver},LIN,1250,500"
        ./specrecv "${LOCAL_RECV_IP}@${core}" "${px}:12345" \
            "$fname" 4096 $psr_acc 1 0-4095 1024 8192 $total_subints 1 1 "$fits_args" \
            >> "$active_log" 2>&1 &
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
    local active_log="${LOG_ROOT}/active_bb_${ant}.log"
    
    IFS=':' read px py sx sy <<< "${MC_MAP[$ant]}"
    
    # Cleanup only this antenna
    pkill -f "bbrec .*_${ant}_"

    echo "[WORKER] Launching bbrec for $ant (CPU: $core)..." >> "$active_log"
    
    ./bbrec -t --direct-io --file-size 64G --total-size ${total_size_gib} \
        "$basename" \
        "${LOCAL_RECV_IP}@${core}" "${sx}:12345" \
        "${LOCAL_RECV_IP}@$((core+1))" "${sy}:12345" \
        >> "$active_log" 2>&1 &
    pids+=($!)
}

# --- MAIN LOOP ---
pids=()

# Log Setup
for ant in $antennas; do
    if [ "$spec_en" = true ]; then setup_log "$ant" "spec"; fi
    if [ "$psr_en" = true ]; then setup_log "$ant" "psr"; fi
    if [ "$bb_en" = true ]; then setup_log "$ant" "bb"; fi
done

# Launch Loop
for ant in $antennas; do
    # --- STATIC PARITY MAPPING ---
    # CA01/CA03 (Odd)  -> Base CPU 160
    # CA02/CA04 (Even) -> Base CPU 168
    
    ant_num=${ant#CA}
    ant_val=$((10#$ant_num))
    
    if (( ant_val % 2 != 0 )); then
        base_cpu=160
    else
        base_cpu=168
    fi
    
    # Run Spec, PSR, BB with +0, +2, +4 offsets
    if [ "$spec_en" = true ]; then run_mbspec "$ant" "$((base_cpu + 0))"; fi
    if [ "$psr_en" = true ]; then run_specrecv "$ant" "$((base_cpu + 2))"; fi
    if [ "$bb_en" = true ]; then run_bbrec "$ant" "$((base_cpu + 4))"; fi
done

for pid in "${pids[@]}"; do wait $pid; done
echo "[WORKER] Finished."
