"""테스트용 가짜 claude.

설정 폴더의 .fake-auth.json 파일로 로그인 상태를 흉내 내고, 호출된 인자와
CLAUDE_CONFIG_DIR 값을 FAKE_CLAUDE_LOG 파일에 한 줄씩 남긴다.
"""

import json
import os
import sys
import time
from pathlib import Path

config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ["FAKE_DEFAULT_DIR"]
state = Path(config_dir) / ".fake-auth.json"
args = sys.argv[1:]

log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    # 계정 상태 조회가 병렬로 실행되어도 Windows에서 호출 로그가 서로 덮이지 않게 한다.
    lock = Path(log + ".lock")
    deadline = time.monotonic() + 10
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError("fake claude log is locked")
            time.sleep(0.005)
    try:
        with open(log, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"args": args, "config_dir": os.environ.get("CLAUDE_CONFIG_DIR")}) + "\n")
    finally:
        lock.rmdir()

if args[:2] == ["auth", "status"]:
    # 실제 claude처럼 API 키 변수가 있으면 로그인 계정 대신 API 키를 보고한다
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(json.dumps({"loggedIn": True, "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"}))
        sys.exit(0)
    if state.exists():
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "configDirectory": config_dir,
                          **json.loads(state.read_text(encoding="utf-8"))}))
        sys.exit(0)
    print(json.dumps({"loggedIn": False, "authMethod": "none", "configDirectory": config_dir}))
    sys.exit(1)

if args[:2] == ["auth", "login"]:
    email = args[args.index("--email") + 1] if "--email" in args else os.environ.get("FAKE_LOGIN_EMAIL", "user@example.com")
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"email": email, "orgName": "Org", "subscriptionType": "max"}), encoding="utf-8")
    # 실제 claude처럼 로그인하면 계정 정보를 담은 .claude.json이 생긴다
    config = Path(config_dir) / ".claude.json"
    if not config.exists():
        config.write_text(json.dumps({"oauthAccount": {"emailAddress": email}, "projects": {}}), encoding="utf-8")
    sys.exit(0)

if args[:2] == ["auth", "logout"]:
    if state.exists():
        state.unlink()
    sys.exit(0)

sys.exit(int(os.environ.get("FAKE_EXIT_CODE", "0")))
