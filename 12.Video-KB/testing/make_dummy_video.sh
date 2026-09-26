#!/usr/bin/env bash
# Regenerate talk.mp4 from dummy_talk.txt.
# The repo already ships talk.mp4 - you only need this to change the narration.
#
# The timecode and section label are burned into the picture on purpose: that is
# what makes a deep link verifiable by eye. Seek to 0:45 and the frame says 0:45.
set -euo pipefail

cd "$(dirname "$0")"
OUT="talk.mp4"

command -v ffmpeg >/dev/null || { echo "ffmpeg is required"; exit 1; }

if command -v say >/dev/null; then
    say -v Samantha -r 160 -o audio.aiff -f dummy_talk.txt
    AUDIO=audio.aiff
elif command -v espeak-ng >/dev/null; then
    espeak-ng -s 150 -f dummy_talk.txt -w audio.wav
    AUDIO=audio.wav
else
    echo "need macOS 'say' or 'espeak-ng' to synthesize speech"; exit 1
fi

FONT=""
for candidate in \
    /System/Library/Fonts/Supplemental/Arial.ttf \
    /System/Library/Fonts/Helvetica.ttc \
    /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf; do
    if [ -f "$candidate" ]; then
        FONT="$candidate"
        break
    fi
done
[ -n "$FONT" ] || { echo "no usable font found"; exit 1; }

label() {
    echo "drawtext=fontfile=${FONT}:text='$1':enable='between(t,$2,$3)':fontcolor=0x8ab4f8:fontsize=52:x=(w-tw)/2:y=h/2+70"
}

FILTER="drawtext=fontfile=${FONT}:text='VIDEO KB TEST CLIP':fontcolor=0x6b7688:fontsize=30:x=(w-tw)/2:y=90"
FILTER="${FILTER},drawtext=fontfile=${FONT}:text='%{pts\\:hms}':fontcolor=white:fontsize=120:x=(w-tw)/2:y=h/2-90"
FILTER="${FILTER},$(label 'Hiring'            0   47)"
FILTER="${FILTER},$(label 'Pricing'           47  95)"
FILTER="${FILTER},$(label 'Database migration' 95 118)"
FILTER="${FILTER},$(label 'Incidents'         118 142)"
FILTER="${FILTER},$(label 'Next quarter'      142 999)"

ffmpeg -y -loglevel error \
    -f lavfi -i "color=c=0x1e2430:s=1280x720:r=15" \
    -i "$AUDIO" -shortest \
    -vf "$FILTER" \
    -c:v libx264 -preset veryfast -pix_fmt yuv420p \
    -c:a aac -b:a 96k "$OUT"

rm -f audio.aiff audio.wav
echo "wrote $OUT ($(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$OUT")s)"
