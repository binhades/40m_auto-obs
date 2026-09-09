
function issue_arm() {
	local roach=$1
	case $2 in
	0) unit=1 ;;
	1) unit=2 ;;
	*) unit=3 ;;
	esac
	nc $roach 7147 > /dev/null 2>&1 <<_EOF_
?wordwrite arm 0 0
?wordwrite arm 0 $unit
_EOF_
}

function noisecal_set() {
	local roach=$1
	shift
	nc $roach 7147 > /dev/null <<_EOF_
?wordwrite noisecal_delay_hipart 0 $1
?wordwrite noisecal_delay 0 $2
?wordwrite noisecal_on_hipart 0 $3
?wordwrite noisecal_on 0 $4
?wordwrite noisecal_off_hipart 0 $5
?wordwrite noisecal_off 0 $6
_EOF_
}

function noisecal_usage() {
	echo "Usage: noisecal [ on | off | mod <delay> <noise_on> <noise_off> ]"
}

NOISECTL_BOARD=r1745

function noisecal() {
	case $1 in
		on)
			noisecal_set $NOISECTL_BOARD 0 0 0xFFFF 0xFFFFFFFF 0 0
			;;
		off)
			noisecal_set $NOISECTL_BOARD 0 0 0 0 0 0
			;;
		mod)
			if [ $# -eq 4 ]; then
				noisecal_set $NOISECTL_BOARD 0 $2 0 $3 0 $4
			else
				noisecal_usage
			fi
			;;
		modx)
			if [ $# -eq 7 ]; then
				noisecal_set $NOISECTL_BOARD $2 $3 $4 $5 $6 $7
			else
				noisecal_usage
			fi
			;;
		*)
			noisecal_usage
			;;
	esac
}

# ATTENTION: count_down_to is finished one second early compare to target time
function count_down_to() {
	local target=$(( $(date +%s -d "$@") - 1 ))
	local now=$(date +%s)
	while [[ $now < $target ]]; do
		sleep 0.1
		printf "\rschedule: $@  waiting: %5d seconds" $((target - now))
		now=$(date +%s)
	done
	echo
}

function nearest_half_second() {
	local subsec='0'
	while [[ ${subsec:0:1} != '5' ]]; do
		sleep 0.01
		subsec=$(date +%N)
	done
}

function count_down_to_exact() {
	local target=$(date +%s -d "$@")
	local now=$(date +%s)
	while [[ $now < $target ]]; do
		sleep 0.00001
		printf "\rschedule: $@  waiting: %5d seconds" $((target - now))
		now=$(date +%s)
	done
	echo
}

# convert d(day) h(hour) m(minite) s(second) 12d34h56m78s into seconds
function dhms2sec() {
	# loop each character to calculate total seconds
	local total=0
	local n=0
	for (( i = 0; i < ${#1}; i++ )); do
		c=${1:$i:1}
		case $c in
			[0-9])
				let n=$n*10+$c
				;;
			d)
				let total+=86400*$n
				let n=0
				;;
			h)
				let total+=3600*$n
				let n=0
				;;
			m)
				let total+=60*$n
				let n=0
				;;
			s)
				let total+=$n
				let n=0
				;;
			*)
				echo -e "Invalid character \"$c\" in string \"$1\""
				exit
				;;
		esac
	done
	# in case of no unit suffix, treat as seconds
	[ $total -eq 0 ] && [ $n -ne 0 ] && total=$n
	echo ${total}
}
