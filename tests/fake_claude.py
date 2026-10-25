"""테스트용 가짜 claude.

설정 폴더의 .fake-auth.json 파일로 로그인 상태를 흉내 내고, 호출된 인자와
CLAUDE_CONFIG_DIR 값을 FAKE_CLAUDE_LOG 파일에 한 줄씩 남긴다.
"""

import json
import os
import sys
from pathlib import Path

config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ["FAKE_DEFAULT_DIR"]
state = Path(config_dir) / ".fake-auth.json"
args = sys.argv[1:]

log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": args, "config_dir": os.environ.get("CLAUDE_CONFIG_DIR")}) + "\n")

if args[:2] == ["auth", "status"]:
    # 실제 claude처럼 API 키 변수가 있으면 로그인 계정 대신 API 키를 보고한다
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(json.dumps({"loggedIn": True, "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"}))
        sys.exit(0)
    if state.exists():
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "configDirectory": config_dir,
                          **json.loads(state.read_text())}))
        sys.exit(0)
    print(json.dumps({"loggedIn": False, "authMethod": "none", "configDirectory": config_dir}))
    sys.exit(1)

if args[:2] == ["auth", "login"]:
    email = args[args.index("--email") + 1] if "--email" in args else os.environ.get("FAKE_LOGIN_EMAIL", "user@example.com")
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"email": email, "orgName": "Org", "subscriptionType": "max"}))
    sys.exit(0)

if args[:2] == ["auth", "logout"]:
    if state.exists():
        state.unlink()
    sys.exit(0)

sys.exit(0)
