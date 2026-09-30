#!/usr/bin/env bash
# 用法：tools/build.sh <编号-名字> <diagram_type> <录制毫秒> [dark|light]
# 例：  tools/build.sh 01-overview architecture 6500 dark
# 流程：JSON -> 校验并交付 HTML -> 无界面 Chrome 录 WebM -> ffmpeg 转 GIF（宽 1000px）
set -euo pipefail
NAME=$1; TYPE=$2; DUR=$3; THEME=${4:-dark}
HERE=$(cd "$(dirname "$0")/.." && pwd)
ARCHIFY=${ARCHIFY_DIR:-/c/Users/xbl26/.claude/skills/archify}
SRC="$HERE/src/$NAME.$TYPE.json"
HTML="$HERE/src/$NAME.html"
node "$ARCHIFY/bin/archify.mjs" deliver "$TYPE" "$SRC" "$HTML" --quality standard --json > "$HERE/src/$NAME.receipt.json"
node "$HERE/tools/record.mjs" "$HTML" "$HERE/src/$NAME-$THEME.webm" "$THEME" "$DUR" "$HERE/src/$NAME-$THEME.png"
ffmpeg -v error -y -i "$HERE/src/$NAME-$THEME.webm" \
  -vf "fps=15,scale=1000:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4" \
  "$HERE/$NAME-$THEME.gif"
ls -la "$HERE/$NAME-$THEME.gif"
