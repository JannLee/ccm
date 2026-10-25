#!/bin/sh
# ccm 명령을 ~/.local/bin 에 심볼릭 링크로 연결한다. 폴더를 옮긴 뒤에는 다시 실행한다.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)/ccm"
DEST_DIR="${CCM_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$DEST_DIR"
chmod +x "$SRC"
ln -sf "$SRC" "$DEST_DIR/ccm"
echo "연결했습니다: $DEST_DIR/ccm -> $SRC"
case ":$PATH:" in
  *":$DEST_DIR:"*) ;;
  *) echo "주의: $DEST_DIR 가 PATH에 없습니다. 셸 설정 파일에 PATH를 추가하세요." ;;
esac
echo "다음 단계: ccm setup  (상태줄 연결과 셸 함수 설치)"
