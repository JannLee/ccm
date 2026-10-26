"""ccm 동작 테스트.

실제 키체인과 ~/.claude는 건드리지 않는다. 프로젝트의 _workspace/tmp 아래에
임시 폴더를 만들고 가짜 claude(tests/fake_claude.py)로 로그인 상태를 흉내 낸다.
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

WINDOWS = os.name == "nt"
if not WINDOWS:
    import fcntl
    import pty
    import termios

# 테스트가 ccm을 불러올 때 프로젝트 폴더에 __pycache__를 남기지 않게 한다
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
CCM = ROOT / "ccm"
FAKE = ROOT / "tests" / "fake_claude.py"
TMP_BASE = ROOT / "_workspace" / "tmp"


def load_module():
    loader = importlib.machinery.SourceFileLoader("ccm_module", str(CCM))
    spec = importlib.util.spec_from_loader("ccm_module", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class PtySession:
    """ccm을 가상 터미널에서 실행하고 키 입력을 보낸다."""

    def __init__(self, args, env):
        self.master, slave = pty.openpty()
        self.p = subprocess.Popen([sys.executable, str(CCM), *args], env=env,
                                  stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                                  preexec_fn=self._take_terminal)
        os.close(slave)
        self.out = b""
        self.pos = 0

    @staticmethod
    def _take_terminal():
        # 실제 터미널처럼 가상 터미널을 제어 터미널로 삼아야 Ctrl+C가 SIGINT로 전달된다
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    def _pump(self, timeout):
        """출력을 읽어 쌓는다. 터미널이 닫혔으면 False를 돌려준다."""
        if not select.select([self.master], [], [], timeout)[0]:
            return True
        try:
            chunk = os.read(self.master, 4096)
        except OSError:
            return False
        self.out += chunk
        return bool(chunk)

    def expect(self, text, timeout=20):
        """마지막으로 찾은 위치 뒤에서 text가 나올 때까지 기다린다."""
        needle = text.encode()
        deadline = time.time() + timeout
        while True:
            i = self.out.find(needle, self.pos)
            if i >= 0:
                self.pos = i + len(needle)
                return
            if time.time() > deadline or not self._pump(0.2):
                raise AssertionError(f"'{text}'이(가) 출력되지 않았습니다:\n{self.text()}")

    def expect_row(self, row):
        """커서(❯)가 있는 행이 row인 화면이 그려질 때까지 기다린다. NO_COLOR라 반전 코드는 없다."""
        self.expect(f"❯  {'●' if row.endswith('default') else ' '}  {row}")

    def send(self, data):
        os.write(self.master, data)

    def close(self, timeout=20):
        deadline = time.time() + timeout
        while self.p.poll() is None and time.time() < deadline:
            self._pump(0.2)
        while self._pump(0.05) and select.select([self.master], [], [], 0)[0]:
            pass
        self.p.wait(timeout=5)
        os.close(self.master)
        return self.text()

    def text(self):
        return self.out.decode("utf-8", "replace")


class CcmTest(unittest.TestCase):
    def setUp(self):
        TMP_BASE.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="test-", dir=TMP_BASE))
        self.claude_home = self.root / "claude"
        (self.claude_home / "skills" / "demo").mkdir(parents=True)
        (self.claude_home / "projects" / "p1").mkdir(parents=True)
        (self.claude_home / "projects" / "p1" / "s.jsonl").write_text("{}\n")
        (self.claude_home / "CLAUDE.md").write_text("# 지침\n", encoding="utf-8")
        self.settings = self.claude_home / "settings.json"
        previous = "powershell.exe -NoProfile -Command 'Write-Output base'" if WINDOWS else "printf base"
        self.settings.write_text(json.dumps(
            {"model": "opus", "statusLine": {"type": "command", "command": previous, "padding": 0}},
            indent=2) + "\n")
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / ("claude.cmd" if WINDOWS else "claude")
        fake.write_text((f'@echo off\n"{sys.executable}" -X utf8 "{FAKE}" %*\n' if WINDOWS
                         else f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n'), encoding="utf-8")
        fake.chmod(0o755)
        self.log = self.root / "claude.log"
        self.local_bin = self.root / "local-bin"
        self.local_bin.mkdir()
        drop = {"CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN", "CCM_SHELL_HOOK", "CLAUDECODE"}
        self.env = {k: v for k, v in os.environ.items() if k not in drop}
        self.env.update(
            CCM_HOME=str(self.root / "ccm"),
            CCM_CLAUDE_HOME=str(self.claude_home),
            CCM_CLAUDE_BIN=str(FAKE if WINDOWS else fake),
            CCM_BIN_DIR=str(self.local_bin),
            CCM_LANG="ko",   # 기존 테스트는 한국어 출력을 확인한다. 영어 출력은 따로 확인한다
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            CCM_POWERSHELL_PROFILE=str(self.home / "profile.ps1"),
            PYTHONUTF8="1",
            SHELL="/bin/zsh",
            FAKE_CLAUDE_LOG=str(self.log),
            FAKE_DEFAULT_DIR=str(self.claude_home),
            NO_COLOR="1",
            COLUMNS="140",
            PATH=os.pathsep.join([str(self.local_bin), str(self.bin), os.environ.get("PATH", "")]),
        )

    def tearDown(self):
        assert self.root.resolve().parent == TMP_BASE.resolve()
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------- 도우미 ----------

    def ccm(self, *args, env=None, input=None, code=0):
        r = subprocess.run([sys.executable, str(CCM), *args], env=env or self.env, input=input,
                           capture_output=True, encoding="utf-8", timeout=60)
        if code is not None and r.returncode != code:
            self.fail(f"ccm {' '.join(args)} → {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        return r

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def pdir(self, name):
        return self.root / "ccm" / "profiles" / name

    def assert_shared(self, path):
        src = self.claude_home / path.name
        self.assertTrue(path.samefile(src), str(path))
        if src.is_dir():
            self.assertTrue(path.is_symlink() or load_module().is_junction(path), str(path))

    def unlink_shared(self, path):
        if WINDOWS and load_module().is_junction(path):
            path.rmdir()
        else:
            path.unlink()

    def rc_path(self):
        return self.home / ("profile.ps1" if WINDOWS else ".zshrc")

    def config(self):
        return json.loads((self.root / "ccm" / "config.json").read_text())

    def write_usage(self, name, windows, updated_at=None):
        path = self.root / "ccm" / "usage" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"windows": windows, "updated_at": updated_at or time.time()}))

    def write_claude_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def write_default_claude_json(self):
        self.write_claude_json(self.home / ".claude.json", {
            "mcpServers": {"a": {"command": "a"}, "b": {"command": "b"}},
            "projects": {"/p1": {"hasTrustDialogAccepted": True, "mcpServers": {"l": {"command": "l"}}},
                         "/p2": {"hasTrustDialogAccepted": False}},
        })

    # ---------- 프로필 추가 ----------

    def test_add_links_shared_items_and_logs_in(self):
        r = self.ccm("add", "work", "--email", "w@example.com")
        d = self.pdir("work")
        for item in ("settings.json", "CLAUDE.md", "skills", "projects"):
            self.assert_shared(d / item)
        self.assertEqual(self.config()["profiles"]["work"]["info"]["email"], "w@example.com")
        self.assertIn("로그인 완료: work → w@example.com", r.stdout)
        login = [c for c in self.calls() if c["args"][:2] == ["auth", "login"]]
        self.assertEqual(login[0]["config_dir"], str(d))
        self.assertIn("--email", login[0]["args"])

    def test_add_creates_missing_state_dirs_before_linking(self):
        self.assertFalse((self.claude_home / "tasks").exists())
        self.ccm("add", "work")
        for item in ("tasks", "teams", "sessions", "paste-cache", "session-env", "todos", "plans"):
            self.assertTrue((self.claude_home / item).is_dir(), item)
            self.assert_shared(self.pdir("work") / item)

    def test_add_shares_user_config_dirs_and_loop_file(self):
        (self.claude_home / "loop.md").write_text("loop\n")
        self.ccm("add", "solo", "--no-share-history")
        d = self.pdir("solo")
        for item in ("rules", "workflows", "themes", "agent-memory", "loop.md"):
            self.assert_shared(d / item)
        self.assertTrue((self.claude_home / "agent-memory").is_dir())

    def test_add_copies_mcp_servers_and_trust_from_default(self):
        self.write_default_claude_json()
        r = self.ccm("add", "work")
        self.assertIn("default 계정에서 가져온 설정: MCP 서버 2개 추가(a, b) · 신뢰한 폴더 1개", r.stdout)
        data = json.loads((self.pdir("work") / ".claude.json").read_text())
        self.assertEqual(data["oauthAccount"]["emailAddress"], "user@example.com")
        self.assertEqual(set(data["mcpServers"]), {"a", "b"})
        p1 = data["projects"]["/p1"]
        self.assertTrue(p1["hasTrustDialogAccepted"])
        self.assertEqual(p1["allowedTools"], [])  # Claude Code가 읽는 기본 키를 채워 둔다
        self.assertEqual(p1["mcpServers"], {"l": {"command": "l"}})
        self.assertNotIn("/p2", data["projects"])

        self.ccm("add", "solo", "--no-sync")
        self.assertNotIn("mcpServers", json.loads((self.pdir("solo") / ".claude.json").read_text()))

    def test_split_state_is_moved_back_and_linked(self):
        self.ccm("add", "work")
        d, shared = self.pdir("work"), self.claude_home / "tasks"
        # Claude Code가 계정 폴더 안에 tasks를 따로 만든 상황을 흉내 낸다
        self.unlink_shared(d / "tasks")
        (d / "tasks" / "session-aaa").mkdir(parents=True)
        (d / "tasks" / "session-aaa" / "1.json").write_text("work")
        (d / "tasks" / "session-bbb").mkdir()
        (d / "tasks" / "note.json").write_text("work")
        (d / "tasks" / ".DS_Store").write_text("x")
        (shared / "session-aaa").mkdir()
        (shared / "session-aaa" / "2.json").write_text("main")
        (shared / "note.json").write_text("main")

        r = self.ccm("ls")
        self.assertIn("[work] ~/.claude와 공유하도록 연결했습니다: tasks(따로 있던 3개 옮김)", r.stderr)
        self.assert_shared(d / "tasks")
        self.assertEqual(sorted(p.name for p in (shared / "session-aaa").iterdir()), ["1.json", "2.json"])
        self.assertTrue((shared / "session-bbb").is_dir())
        self.assertEqual((shared / "note.json").read_text(), "main")
        self.assertEqual((shared / "note.json.work").read_text(), "work")
        self.assertFalse((shared / ".DS_Store").exists())
        # 다시 실행하면 바꿀 것이 없다
        self.assertEqual(self.ccm("ls").stderr, "")

    def test_doctor_finds_split_state_dirs(self):
        self.ccm("add", "work")
        d = self.pdir("work")
        self.unlink_shared(d / "sessions")
        (d / "sessions").mkdir()
        (d / "sessions" / "123.json").write_text("{}")
        out = self.ccm("doctor", code=None).stdout
        self.assertIn("[work] sessions: 계정 폴더에 따로 있어 공유되지 않음 (`ccm doctor --fix`)", out)
        out = self.ccm("doctor", "--fix", code=None).stdout
        self.assertIn("sessions(따로 있던 1개 옮김)", out)
        self.assertNotIn("[work] sessions:", out)
        self.assertTrue((self.claude_home / "sessions" / "123.json").exists())

    def test_add_without_history_sharing(self):
        self.ccm("add", "solo", "--no-share-history")
        d = self.pdir("solo")
        self.assert_shared(d / "settings.json")
        self.assertFalse((d / "projects").exists())

    def test_add_rejects_bad_or_duplicate_names(self):
        self.assertIn("default", self.ccm("add", "default", code=1).stderr)
        self.ccm("add", "../x", code=1)
        self.ccm("add", "work")
        self.assertIn("이미 있습니다", self.ccm("add", "work", code=1).stderr)

    def test_add_warns_when_same_account_is_registered_twice(self):
        self.ccm("add", "a", "--email", "same@example.com")
        r = self.ccm("add", "b", "--email", "same@example.com")
        self.assertIn("a 프로필과 같은 계정", r.stdout)

    # ---------- 사용량 표 ----------

    def test_ls_shows_accounts_and_reset_windows(self):
        self.ccm("add", "work", "--email", "w@example.com")
        now = time.time()
        self.write_usage("work", {
            "five_hour": {"used_percentage": 95.0, "resets_at": now - 60},
            "seven_day": {"used_percentage": 40.0, "resets_at": now + 86400},
        })
        out = self.ccm("ls").stdout
        self.assertIn("w@example.com", out)
        self.assertIn("로그인 필요", out)  # default 프로필은 가짜 claude에서 로그인 전 상태
        work_line = next(line for line in out.splitlines() if " work " in line)
        self.assertIn("초기화됨", work_line)
        self.assertIn(" 40%", work_line)
        data = {p["name"]: p for p in json.loads(self.ccm("ls", "--json").stdout)}
        self.assertEqual(data["work"]["five_hour"], {"used_percentage": 0.0, "resets_at": None, "reset_passed": True})
        self.assertEqual(data["work"]["seven_day"]["used_percentage"], 40.0)
        self.assertIsNone(data["default"]["five_hour"])

    def test_ls_drops_account_column_on_narrow_terminal(self):
        self.ccm("add", "work", "--email", "w@example.com")
        out = self.ccm("ls", env={**self.env, "COLUMNS": "60"}).stdout
        self.assertNotIn("w@example.com", out)
        self.assertIn("work", out)

    def test_api_key_variable_does_not_hide_accounts(self):
        self.ccm("add", "work", "--email", "w@example.com")
        self.assertNotIn("ANTHROPIC_API_KEY", self.ccm("ls").stdout)

        # 셸에 변수가 남아 있으면 표는 로그인 계정을 그대로 보여 주고 unset 방법을 안내한다
        out = self.ccm("ls", env={**self.env, "ANTHROPIC_API_KEY": "x"}).stdout
        self.assertIn("w@example.com", out)
        self.assertNotIn("API 키 사용 중", out)
        self.assertIn("Remove-Item Env:ANTHROPIC_API_KEY" if WINDOWS else "unset ANTHROPIC_API_KEY", out)

        # Claude Code 세션 안에서 실행하면 세션에서 물려받은 값이라고 안내한다
        out = self.ccm("ls", env={**self.env, "ANTHROPIC_API_KEY": "x", "CLAUDECODE": "1"}).stdout
        self.assertIn("Claude Code 세션 안에서 실행해서", out)
        self.assertNotIn("unset", out)

    def test_unreadable_config_is_left_untouched(self):
        self.ccm("add", "work")
        path = self.root / "ccm" / "config.json"
        path.write_text('{"profiles": {"work": ')
        self.assertIn("읽지 못했습니다", self.ccm("ls", code=1).stderr)
        self.assertIn("읽지 못했습니다", self.ccm("use", "default", code=1).stderr)
        self.assertEqual(path.read_text(), '{"profiles": {"work": ')
        # 상태줄은 설정 파일이 깨져도 계속 출력한다
        self.assertEqual(self.ccm("statusline", input="{}").stdout, "[default]")

    def test_ls_warns_when_statusline_is_not_connected(self):
        self.assertIn("ccm setup", self.ccm("ls").stdout)
        self.ccm("setup", "--no-shell")
        self.assertNotIn("상태줄이 연결되어 있지 않아", self.ccm("ls").stdout)

    def test_broken_statusline_command_is_reported_and_fixed(self):
        self.ccm("setup", "--no-shell")
        moved = self.root / "old-place" / "ccm"
        settings = json.loads(self.settings.read_text())
        settings["statusLine"]["command"] = f"{shlex.quote(Path(sys.executable).as_posix())} {shlex.quote(moved.as_posix())} statusline"
        self.settings.write_text(json.dumps(settings))
        self.assertIn("`./ccm doctor --fix`", self.ccm("ls").stdout)
        self.assertIn(f"{moved.as_posix()} 파일이 없어", self.ccm("doctor", code=1).stdout)
        self.ccm("doctor", "--fix", code=None)
        self.assertNotIn(str(moved), json.loads(self.settings.read_text())["statusLine"]["command"])
        self.assertNotIn("사용량이 기록되지 않습니다", self.ccm("ls").stdout)

        settings = json.loads(self.settings.read_text())
        settings["statusLine"]["command"] = f"/nonexistent/python3 {shlex.quote(CCM.as_posix())} statusline"
        self.settings.write_text(json.dumps(settings))
        self.assertIn("Python(/nonexistent/python3)이 없어", self.ccm("ls").stdout)

    def test_doctor_relinks_install_link_after_folder_move(self):
        if WINDOWS:
            link = self.local_bin / "ccm.cmd"
            link.write_text('@echo off\n"C:\\old-place\\python.exe" ccm %*\n', encoding="utf-8")
            self.assertIn("Windows 실행 파일 설치 또는 갱신 필요", self.ccm("doctor", code=1).stdout)
            self.ccm("doctor", "--fix", code=None)
            self.assertIn(str(CCM), link.read_text(encoding="utf-8"))
            self.assertNotIn("Windows 실행 파일 설치 또는 갱신 필요", self.ccm("doctor", code=None).stdout)
            return
        link = self.local_bin / "ccm"
        link.symlink_to(self.root / "old-place" / "ccm")
        self.assertIn(f"{link} 링크가 가리키는", self.ccm("doctor", code=1).stdout)
        out = self.ccm("doctor", "--fix", code=None).stdout
        self.assertIn("ccm 링크를 다시 만들었습니다", out)
        self.assertIn(f"✓ ccm 명령: {link}", out)
        self.assertEqual(link.resolve(), CCM.resolve())

    # ---------- 상태줄 ----------

    def test_statusline_records_usage_and_keeps_previous_output(self):
        self.ccm("add", "work")
        self.ccm("setup", "--no-shell")
        settings = json.loads(self.settings.read_text())
        command = settings["statusLine"]["command"]
        self.assertTrue(load_module().is_ccm_statusline({"command": command}))
        self.assertEqual(settings["statusLine"]["padding"], 0)
        self.assertEqual(settings["model"], "opus")
        self.assertTrue(list(self.claude_home.glob("settings.json.ccm-backup-*")))

        now = time.time()
        payload = {"rate_limits": {
            "five_hour": {"used_percentage": 72.4, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 31, "resets_at": now + 3 * 86400},
        }}
        env = {**self.env, "CLAUDE_CONFIG_DIR": str(self.pdir("work"))}
        r = subprocess.run(command, shell=True, input=json.dumps(payload), env=env,
                           capture_output=True, encoding="utf-8", timeout=30)
        self.assertTrue(r.stdout.startswith("base  [work] 5h 72% ↻"), r.stdout)
        self.assertIn("· 7d 31% ↻", r.stdout)
        rec = json.loads((self.root / "ccm" / "usage" / "work.json").read_text())
        self.assertEqual(rec["windows"]["five_hour"]["used_percentage"], 72.4)

        # rate_limits가 없는 호출(세션 첫 응답 전)은 마지막 기록을 보여 주고 기록은 바꾸지 않는다
        r = subprocess.run(command, shell=True, input="{}", env=env, capture_output=True, encoding="utf-8")
        self.assertIn("[work] 5h 72%", r.stdout)

    def test_statusline_ignores_older_values_from_idle_sessions(self):
        self.ccm("add", "work")
        env = {**self.env, "CLAUDE_CONFIG_DIR": str(self.pdir("work"))}
        usage = self.root / "ccm" / "usage" / "work.json"
        reset = time.time() + 3600

        def send(pct, resets_at):
            payload = {"rate_limits": {"five_hour": {"used_percentage": pct, "resets_at": resets_at}}}
            return self.ccm("statusline", env=env, input=json.dumps(payload)).stdout

        def saved():
            rec = json.loads(usage.read_text())
            return rec["windows"]["five_hour"]["used_percentage"], rec["updated_at"]

        send(60, reset)
        first = saved()
        # 쉬고 있던 세션이 같은 창의 예전 값으로 상태줄을 다시 그려도 기록과 표시는 최신 값을 유지한다
        self.assertIn("5h 60%", send(30, reset))
        self.assertEqual(saved(), first)
        # 이미 끝난 창의 값도 기록하지 않는다
        send(90, reset - 5 * 3600)
        self.assertEqual(saved(), first)
        # 새 창이 시작되면 더 낮은 값이라도 기록한다
        self.assertIn("5h 5%", send(5, reset + 5 * 3600))
        self.assertEqual(saved()[0], 5)

    def test_statusline_for_default_and_unknown_dirs(self):
        payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": time.time() + 60}}})
        # setup 전에는 감쌀 이전 명령이 없으므로 ccm 출력만 나온다
        r = self.ccm("statusline", input=payload)
        self.assertRegex(r.stdout, r"^\[default\] 5h 10% ↻\d\d:\d\d$")
        self.assertTrue((self.root / "ccm" / "usage" / "default.json").exists())

        other = self.root / "elsewhere"
        r = self.ccm("statusline", env={**self.env, "CLAUDE_CONFIG_DIR": str(other)}, input=payload)
        self.assertIn("[elsewhere]", r.stdout)
        self.assertFalse((self.root / "ccm" / "usage" / "elsewhere.json").exists())

    # ---------- 전환 ----------

    def test_use_and_shell_function(self):
        self.ccm("add", "work")
        self.ccm("setup")
        rc = self.rc_path()

        def run_claude(extra_env=None):
            env = {**self.env, **(extra_env or {})}
            if WINDOWS:
                cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                       f". {load_module().ps_quote(rc)}; claude hello"]
            else:
                shell = "zsh" if shutil.which("zsh") else "bash"
                cmd = [shell, "-f", "-c", f"source {shlex.quote(str(rc))}; claude hello"]
            subprocess.run(cmd, env=env, check=True, timeout=30)
            return self.calls()[-1]

        self.assertIn("셸 함수", self.ccm("use", "work").stdout)
        self.assertEqual((self.root / "ccm" / "current-dir").read_text().strip(), str(self.pdir("work")))
        self.assertEqual(run_claude(), {"args": ["hello"], "config_dir": str(self.pdir("work"))})

        # 셸에 CLAUDE_CONFIG_DIR을 직접 지정하면 그 값을 따른다
        self.assertEqual(run_claude({"CLAUDE_CONFIG_DIR": "/x"})["config_dir"], "/x")

        self.ccm("use", "default")
        self.assertEqual((self.root / "ccm" / "current-dir").read_text(), "")
        self.assertIsNone(run_claude()["config_dir"])

    def test_resolve_by_number_prefix_and_email(self):
        self.ccm("add", "work", "--email", "w@example.com")
        self.ccm("add", "web", "--email", "b@corp.example")
        self.assertIn("여러 개", self.ccm("use", "w", code=1).stderr)
        self.ccm("use", "3")
        self.assertEqual(self.config()["current"], "web")
        self.ccm("use", "wo")
        self.assertEqual(self.config()["current"], "work")
        self.ccm("use", "corp")
        self.assertEqual(self.config()["current"], "web")
        self.assertIn("찾지 못했습니다", self.ccm("use", "nope", code=1).stderr)
        self.assertIn("4번 계정이 없습니다", self.ccm("use", "4", code=1).stderr)

    def test_run_launches_once_without_changing_current(self):
        self.ccm("add", "work")
        self.ccm("run", "work", "-c", "--model", "sonnet")
        self.assertEqual(self.calls()[-1], {"args": ["-c", "--model", "sonnet"], "config_dir": str(self.pdir("work"))})
        self.assertEqual(self.config()["current"], "default")

    def test_next_switches_to_account_with_most_headroom(self):
        for name in ("work", "side", "spare"):
            self.ccm("add", name, "--email", f"{name}@example.com")
        self.ccm("use", "work")
        now = time.time()
        self.write_usage("work", {"five_hour": {"used_percentage": 100.0, "resets_at": now + 600}})
        self.write_usage("side", {"five_hour": {"used_percentage": 20.0, "resets_at": now + 600},
                                  "seven_day": {"used_percentage": 85.0, "resets_at": now + 86400}})
        self.write_usage("spare", {"five_hour": {"used_percentage": 40.0, "resets_at": now + 600},
                                   "seven_day": {"used_percentage": 10.0, "resets_at": now + 86400}})
        out = self.ccm("ls").stdout
        self.assertIn("한도", next(line for line in out.splitlines() if "2  work" in line))
        self.assertIn("여유가 가장 많은 다른 계정: 4 spare (5시간 40% · 주간 10%) · `ccm next`로 전환", out)

        # 주간 사용률이 높은 side보다 두 창 모두 여유가 있는 spare를 고른다
        r = self.ccm("next", "-c")
        self.assertIn("현재 계정을 spare(으)로 바꿨습니다: 5시간 40% · 주간 10%", r.stdout)
        self.assertEqual(self.calls()[-1], {"args": ["-c"], "config_dir": str(self.pdir("spare"))})
        self.assertEqual(self.config()["current"], "spare")

        # 다음 후보는 현재 계정을 뺀 나머지에서 고른다
        before = len(self.calls())
        self.ccm("next", "-n")
        self.assertEqual(self.config()["current"], "side")
        self.assertFalse([c for c in self.calls()[before:] if c["args"][:1] != ["auth"]])

    def test_next_reports_when_every_other_account_is_limited(self):
        self.ccm("add", "work", "--email", "w@example.com")
        self.ccm("add", "side", "--email", "s@example.com")
        self.ccm("use", "work")
        self.write_usage("side", {"seven_day": {"used_percentage": 100.0, "resets_at": time.time() + 7200}})
        r = self.ccm("next", "-n", code=1)
        self.assertIn("다른 계정도 모두 한도에 도달했습니다. 가장 먼저 풀리는 계정은 side(↻", r.stderr)
        self.assertEqual(self.config()["current"], "work")

    def test_profiles_of_same_account_share_latest_usage(self):
        self.ccm("add", "a", "--email", "same@example.com")
        self.ccm("add", "b", "--email", "same@example.com")
        self.ccm("add", "c", "--email", "other@example.com")
        now = time.time()
        week = {"resets_at": now + 86400}
        self.write_usage("a", {"seven_day": {**week, "used_percentage": 5.0}}, updated_at=now - 3 * 86400)
        self.write_usage("b", {"seven_day": {**week, "used_percentage": 70.0}}, updated_at=now - 60)
        self.write_usage("c", {"seven_day": {**week, "used_percentage": 80.0}})
        # 같은 계정이면 한도를 함께 쓰므로 오래된 a의 기록 대신 b의 최근 기록을 보여 준다
        data = {p["name"]: p for p in json.loads(self.ccm("ls", "--json").stdout)}
        self.assertEqual(data["a"]["seven_day"]["used_percentage"], 70.0)
        # a에서 바꿀 때 같은 계정인 b는 후보에서 빠진다
        self.ccm("use", "a")
        self.ccm("next", "-n")
        self.assertEqual(self.config()["current"], "c")

    @unittest.skipIf(WINDOWS, "POSIX PTY; Windows console is tested separately")
    def test_pick_moves_with_arrow_keys_and_launches(self):
        self.ccm("add", "work", "--email", "w@example.com")
        self.ccm("add", "side", "--email", "s@example.com")
        s = PtySession(["--", "-c"], self.env)
        s.expect_row("1  default")  # 처음에는 현재 계정에 커서가 있다
        s.expect("Enter 실행")
        s.send(b"\x1b[B")
        s.expect_row("2  work")
        s.send(b"\x1b[B")
        s.expect_row("3  side")
        s.send(b"\x1b[B")
        s.expect_row("1  default")  # 마지막 행에서 내리면 첫 행으로 돌아간다
        s.send(b"\x1b[A")
        s.expect_row("3  side")
        s.send(b"\x1b[A")
        s.expect_row("2  work")
        s.send(b"\r")
        s.expect("→ work 계정으로 claude를 실행합니다.")
        s.close()
        self.assertEqual(self.calls()[-1], {"args": ["-c"], "config_dir": str(self.pdir("work"))})
        self.assertEqual(self.config()["current"], "work")

    @unittest.skipIf(WINDOWS, "POSIX PTY; Windows console is tested separately")
    def test_pick_number_key_moves_cursor(self):
        self.ccm("add", "work")
        self.ccm("add", "side")
        s = PtySession(["-n"], self.env)
        s.expect("Enter 전환")
        s.send(b"3")
        s.expect_row("3  side")
        s.send(b"9")  # 없는 번호는 무시한다
        s.send(b"\r")
        s.expect("현재 계정을 side(으)로 바꿨습니다.")
        s.close()
        self.assertEqual(self.config()["current"], "side")
        self.assertFalse([c for c in self.calls() if not c["args"][:1] == ["auth"]])

    @unittest.skipIf(WINDOWS, "POSIX PTY; Windows console is tested separately")
    def test_pick_esc_asks_before_exit(self):
        self.ccm("add", "work")
        s = PtySession([], self.env)
        s.expect("ESC 종료")
        s.send(b"\x1b")
        s.expect("ccm을 종료할까요? [Y/n]")
        s.send(b"n")  # n이면 선택 화면으로 돌아간다
        s.expect("ESC 종료")
        s.send(b"\x1b")
        s.expect("종료할까요?")
        s.send(b"\x1b")  # 확인 중에 ESC를 다시 눌러도 돌아간다
        s.expect("ESC 종료")
        s.send(b"\x1b[B")
        s.expect_row("2  work")
        s.send(b"q")
        s.expect("종료할까요?")
        s.send(b"y")
        s.expect("종료했습니다.")
        s.close()
        self.assertEqual(s.p.returncode, 0)
        self.assertEqual(self.config()["current"], "default")
        self.assertFalse([c for c in self.calls() if not c["args"][:1] == ["auth"]])

    @unittest.skipIf(WINDOWS, "POSIX PTY; Windows console is tested separately")
    def test_pick_enter_confirms_exit(self):
        s = PtySession([], self.env)
        s.expect("ESC 종료")
        s.send(b"\x1b")
        s.expect("종료할까요?")
        s.send(b"\r")
        s.expect("종료했습니다.")
        s.close()
        self.assertEqual(s.p.returncode, 0)

    @unittest.skipIf(WINDOWS, "POSIX PTY; Windows console is tested separately")
    def test_pick_ctrl_c_exits_without_confirm(self):
        s = PtySession([], self.env)
        s.expect("ESC 종료")
        s.send(b"\x03")
        s.close()
        self.assertEqual(s.p.returncode, 130)

    def test_pick_without_tty_only_prints_table(self):
        r = self.ccm()
        self.assertIn("Claude Code 계정", r.stdout)
        self.assertNotIn("ESC 종료", r.stdout)

    # ---------- 삭제와 되돌리기 ----------

    def test_rm_removes_profile_but_keeps_shared_sources(self):
        self.ccm("add", "work")
        self.ccm("use", "work")
        self.write_usage("work", {"five_hour": {"used_percentage": 1.0, "resets_at": None}})
        self.ccm("rm", "work", input="n\n", code=1)
        self.assertTrue(self.pdir("work").exists())
        self.ccm("rm", "work", "-y")
        self.assertFalse(self.pdir("work").exists())
        self.assertTrue((self.claude_home / "projects" / "p1" / "s.jsonl").exists())
        self.assertTrue((self.claude_home / "skills" / "demo").is_dir())
        self.assertTrue(self.settings.exists())
        self.assertFalse((self.root / "ccm" / "usage" / "work.json").exists())
        logout = [c for c in self.calls() if c["args"][:2] == ["auth", "logout"]]
        self.assertEqual(logout[-1]["config_dir"], str(self.pdir("work")))
        self.assertEqual(self.config()["current"], "default")
        self.assertIn("지울 수 없습니다", self.ccm("rm", "default", "-y", code=1).stderr)

    def test_setup_is_idempotent_and_undo_restores(self):
        rc = self.rc_path()
        rc.write_text("export FOO=1\n")
        original_settings = json.loads(self.settings.read_text())
        self.ccm("setup")
        r = self.ccm("setup")
        self.assertIn("이미 연결", r.stdout)
        self.assertIn("이미 설치", r.stdout)
        self.assertEqual(rc.read_text(encoding="utf-8-sig").count("# >>> ccm >>>"), 1)
        self.ccm("setup", "--undo")
        self.assertEqual(json.loads(self.settings.read_text()), original_settings)
        self.assertEqual(rc.read_text(encoding="utf-8-sig"), "export FOO=1\n")

    def test_setup_without_previous_statusline(self):
        self.settings.write_text(json.dumps({"model": "opus"}) + "\n")
        self.ccm("setup", "--no-shell")
        self.assertIn("statusLine", json.loads(self.settings.read_text()))
        r = self.ccm("statusline", input="{}")
        self.assertEqual(r.stdout, "[default]")
        self.ccm("setup", "--undo")
        self.assertEqual(json.loads(self.settings.read_text()), {"model": "opus"})

    # ---------- 점검 ----------

    def test_doctor_reports_and_fixes_links(self):
        self.ccm("add", "work", "--email", "w@example.com")
        d = self.pdir("work")
        (d / "settings.json").unlink()
        (d / "settings.json").write_text("{}")
        (d / "CLAUDE.md").unlink()
        r = self.ccm("doctor", code=1)
        self.assertIn("settings.json: 별도 파일로 바뀌어 공유되지 않음", r.stdout)
        self.assertIn("CLAUDE.md: 연결 없음", r.stdout)
        self.ccm("doctor", "--fix", code=1)
        self.assert_shared(d / "CLAUDE.md")


    # ---------- 출력 언어 ----------

    def env_en(self):
        return {**self.env, "CCM_LANG": "en"}

    def test_english_output(self):
        en = self.env_en()
        r = self.ccm("add", "work", "--email", "w@example.com", env=en)
        self.assertIn("Logged in: work → w@example.com", r.stdout)
        self.ccm("add", "side", "--email", "s@example.com", env=en)
        now = time.time()
        self.write_usage("work", {"five_hour": {"used_percentage": 100.0, "resets_at": now + 600},
                                  "seven_day": {"used_percentage": 40.0, "resets_at": now - 60}})
        out = self.ccm("ls", env=en).stdout
        self.assertIn("Claude Code accounts · current: default", out)
        for word in ("name", "account", "plan", "5-hour", "weekly", "updated", "login required"):
            self.assertIn(word, out)
        work = next(line for line in out.splitlines() if "2  work" in line)
        self.assertIn("LIMIT", work)
        self.assertIn("reset", work)
        self.assertIn("no data", next(line for line in out.splitlines() if "3  side" in line))
        self.assertIn("Most headroom: 3 side (no data) · switch with `ccm next`", out)
        self.assertNotRegex(out, "[가-힣]")
        self.assertIn("No account matches 'nope'", self.ccm("use", "nope", env=en, code=1).stderr)
        self.assertIn("Switched to side: no data", self.ccm("next", "-n", env=en).stdout)
        doctor = self.ccm("doctor", env=en, code=None).stdout
        self.assertIn("claude executable", doctor)
        self.assertNotRegex(doctor, "[가-힣]")
        self.assertIn("Manage Claude Code account profiles", self.ccm("--help", env=en).stdout)

    def test_language_follows_env_saved_value_and_locale(self):
        base = {k: v for k, v in self.env.items() if k not in ("CCM_LANG", "LC_ALL", "LC_MESSAGES", "LANG")}
        ko_locale, en_locale = {**base, "LANG": "ko_KR.UTF-8"}, {**base, "LANG": "en_US.UTF-8"}
        self.assertIn("출력 언어: 한국어 (로캘 LANG)", self.ccm("lang", env=ko_locale).stdout)
        # 로캘이 ko 가 아니고 시스템 언어도 한국어가 아니면(테스트 HOME 에는 macOS 설정이 없다) 영어
        self.assertIn("Output language: English", self.ccm("lang", env=en_locale).stdout)
        # 저장한 값이 로캘보다 먼저 쓰인다
        self.assertIn("한국어 (`ccm lang`으로 저장한 값)", self.ccm("lang", "ko", env=en_locale).stdout)
        self.assertEqual(self.config()["lang"], "ko")
        self.assertIn("Claude Code 계정 · 현재 default", self.ccm("ls", env=en_locale).stdout)
        # CCM_LANG 이 저장한 값보다 먼저 쓰인다
        self.assertIn("English (CCM_LANG environment variable)", self.ccm("lang", env={**en_locale, "CCM_LANG": "en"}).stdout)
        self.ccm("lang", "auto", env=en_locale)
        self.assertNotIn("lang", self.config())
        self.assertIn("Claude Code accounts", self.ccm("ls", env=en_locale).stdout)

    @unittest.skipIf(WINDOWS, "POSIX PTY; Windows console is tested separately")
    def test_pick_in_english(self):
        self.ccm("add", "work", env=self.env_en())
        s = PtySession([], self.env_en())
        s.expect("↑↓ move · Enter launch · Esc quit")
        s.send(b"\x1b[B")
        s.expect_row("2  work")
        s.send(b"\r")
        s.expect("→ Launching claude as work.")
        s.close()
        self.assertEqual(self.calls()[-1]["config_dir"], str(self.pdir("work")))

    # ---------- 설정 복사 ----------

    def test_sync_keeps_different_servers_unless_replace(self):
        self.ccm("add", "work")
        target = self.pdir("work") / ".claude.json"
        self.write_claude_json(target, {"mcpServers": {"b": {"command": "mine"}}, "other": 1,
                                        "projects": {"/p1": {"allowedTools": ["x"], "hasTrustDialogAccepted": False}}})
        self.write_default_claude_json()

        out = self.ccm("sync", "--dry-run").stdout
        self.assertIn("[work] MCP 서버 1개 추가(a) · 신뢰한 폴더 1개 · 프로젝트별 MCP 서버 1개", out)
        self.assertIn("파일은 바꾸지 않았습니다", out)
        self.assertEqual(json.loads(target.read_text())["mcpServers"], {"b": {"command": "mine"}})

        self.assertIn("그대로 둔 MCP 서버: b", self.ccm("sync").stdout)
        data = json.loads(target.read_text())
        self.assertEqual(data["mcpServers"], {"a": {"command": "a"}, "b": {"command": "mine"}})
        self.assertEqual(data["projects"]["/p1"]["allowedTools"], ["x"])  # 이미 있는 프로젝트 설정은 유지한다
        self.assertTrue(data["projects"]["/p1"]["hasTrustDialogAccepted"])
        self.assertEqual(data["other"], 1)
        self.assertIn("[work] 바꿀 것 없음", self.ccm("sync").stdout)

        self.assertIn("MCP 서버 1개 덮어씀(b)", self.ccm("sync", "work", "--replace").stdout)
        self.assertEqual(json.loads(target.read_text())["mcpServers"]["b"], {"command": "b"})
        self.assertIn("'nope' 계정을 찾지 못했습니다", self.ccm("sync", "--from", "nope", code=1).stderr)

    def test_sync_waits_for_claude_code_lock(self):
        self.ccm("add", "work")
        self.write_default_claude_json()
        lock = self.pdir("work") / ".claude.json.lock"
        # 끝난 프로그램이 남긴 오래된 잠금은 지우고 진행한다
        lock.mkdir()
        os.utime(lock, (time.time() - 60, time.time() - 60))
        self.ccm("sync")
        self.assertFalse(lock.exists())
        # 다른 프로그램이 잡고 있는 잠금은 풀릴 때까지 기다린다
        (self.pdir("work") / ".claude.json").write_text("{}")
        lock.mkdir()
        threading.Timer(1.0, lock.rmdir).start()
        started = time.time()
        self.ccm("sync")
        self.assertGreaterEqual(time.time() - started, 0.9)
        self.assertIn("a", json.loads((self.pdir("work") / ".claude.json").read_text())["mcpServers"])

    # ---------- Windows ----------

    @unittest.skipUnless(WINDOWS, "Windows installer")
    def test_windows_installer_and_both_launchers(self):
        installed = self.local_bin / "한글 & [bin]'"
        source = self.root / "한글 & [repo]' %PATH% !"
        source.mkdir()
        shutil.copyfile(CCM, source / "ccm")
        shutil.copyfile(ROOT / "install.ps1", source / "install.ps1")
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(source / "install.ps1"),
             "-Python", sys.executable, "-BinDir", str(installed), "-NoPath"],
            env=self.env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ([str(installed / "ccm.cmd"), "--version"],
                        ["powershell.exe", "-NoProfile", "-File", str(installed / "ccm.ps1"), "--version"]):
            result = subprocess.run(command, env=self.env, capture_output=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"ccm {load_module().VERSION}", result.stdout)

    @unittest.skipUnless(WINDOWS, "Windows process handling")
    def test_windows_run_propagates_exit_code_and_arguments(self):
        self.ccm("add", "work")
        self.ccm("run", "work", "--model", "value with spaces", env={**self.env, "FAKE_EXIT_CODE": "7"}, code=7)
        self.assertEqual(self.calls()[-1]["args"], ["--model", "value with spaces"])

    @unittest.skipUnless(WINDOWS, "PowerShell environment restoration")
    def test_windows_shell_function_restores_environment_after_failure(self):
        self.ccm("add", "work")
        self.ccm("use", "work")
        self.ccm("setup")
        command = (f". {load_module().ps_quote(self.rc_path())}; "
                   "$ccmBefore = $env:CLAUDE_CONFIG_DIR; claude 'argument with spaces'; "
                   "$ccmResult = $LASTEXITCODE; "
                   "if ($env:CLAUDE_CONFIG_DIR -ne $ccmBefore) { exit 99 }; exit $ccmResult")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                env={**self.env, "FAKE_EXIT_CODE": "7"}, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.calls()[-1], {"args": ["argument with spaces"], "config_dir": str(self.pdir("work"))})

    @unittest.skipUnless(WINDOWS, "Windows path quoting")
    def test_windows_statusline_supports_special_paths_in_both_shells(self):
        location = self.root / "한글 & [repo]'"
        location.mkdir()
        script = location / "ccm"
        shutil.copyfile(CCM, script)
        self.ccm("add", "work")
        setup = subprocess.run([sys.executable, str(script), "setup", "--no-shell"],
                               env=self.env, capture_output=True, timeout=30)
        self.assertEqual(setup.returncode, 0, setup.stderr)
        command = json.loads(self.settings.read_text())["statusLine"]["command"]
        payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 23, "resets_at": time.time() + 600}}})
        env = {**self.env, "CLAUDE_CONFIG_DIR": str(self.pdir("work"))}
        shells = [["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]]
        git = shutil.which("git")
        bash = Path(git).resolve().parent.parent / "bin" / "bash.exe" if git else None
        if bash and bash.exists():
            shells.append([str(bash), "-c", command])
        for argv in shells:
            result = subprocess.run(argv, input=payload, env=env, capture_output=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("base  [work] 5h 23%", result.stdout)

    @unittest.skipUnless(WINDOWS, "Windows hard links")
    def test_windows_repairs_hard_links_after_atomic_save_and_keeps_private_edits(self):
        self.ccm("add", "work")
        replacement = self.settings.with_suffix(".new")
        replacement.write_text('{"model": "sonnet"}', encoding="utf-8")
        os.replace(replacement, self.settings)
        self.ccm("ls")
        self.assert_shared(self.pdir("work") / "settings.json")
        private = self.pdir("work") / "settings.json"
        private.unlink()
        private.write_text('{"model": "private"}', encoding="utf-8")
        self.ccm("ls")
        self.assertEqual(json.loads(private.read_text())["model"], "private")
        self.assertEqual(json.loads(self.settings.read_text())["model"], "sonnet")

    @unittest.skipUnless(WINDOWS, "Windows PowerShell profile encodings")
    def test_windows_preserves_utf16_profile_content(self):
        rc = self.rc_path()
        original = "$ccmNote = '한글'\r\n"
        rc.write_bytes(original.encode("utf-16"))
        self.ccm("setup")
        self.ccm("setup", "--undo")
        self.assertEqual(rc.read_text(encoding="utf-8-sig"), original.replace("\r\n", "\n"))

    @unittest.skipUnless(WINDOWS, "Windows console input")
    def test_windows_picker_handles_arrows_and_cancelled_exit(self):
        self.ccm("add", "work")
        m = load_module()
        output = io.StringIO()
        cfg = self.config()
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m.sys, "stdout", output), \
                mock.patch.object(m.msvcrt, "kbhit", return_value=True), \
                mock.patch.object(m.msvcrt, "getwch", side_effect=["\xe0", "P", "\x1b", "n", "\r"]):
            selected = m.pick_interactive(cfg, True)
        self.assertEqual(selected, "work")
        self.assertEqual(cfg["current"], "work")
        self.assertIn("종료할까요?", output.getvalue())
        self.assertIn("\x1b[?25h", output.getvalue())

    @unittest.skipUnless(WINDOWS, "Windows console cleanup")
    def test_windows_console_mode_restored_on_error(self):
        m = load_module()
        api = mock.Mock()
        api.GetStdHandle.return_value = 123
        def original_mode(handle, mode):
            mode._obj.value = 1
            return 1
        api.GetConsoleMode.side_effect = original_mode
        with mock.patch.object(m, "windows_api", return_value=api):
            with self.assertRaisesRegex(RuntimeError, "interrupt"):
                with m.terminal_mode(0):
                    raise RuntimeError("interrupt")
        self.assertEqual(api.SetConsoleMode.call_args_list, [mock.call(123, 5), mock.call(123, 1)])

    @unittest.skipUnless(WINDOWS, "Windows console interruption")
    def test_windows_picker_restores_cursor_when_interrupted_waiting_for_input(self):
        m = load_module()
        cfg = {"profiles": {"default": {}}, "current": "default"}
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m.sys, "stdout", output), \
                mock.patch.object(m.msvcrt, "kbhit", return_value=False), \
                mock.patch.object(m.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                m.pick_interactive(cfg, True)
        self.assertIn("\x1b[?25h", output.getvalue())
        self.assertEqual(cfg["current"], "default")

    @unittest.skipUnless(WINDOWS, "Windows links without Developer Mode")
    def test_windows_fallback_links_repair_and_remove_without_touching_sources(self):
        m = load_module()
        denied = OSError("symbolic link privilege unavailable")
        denied.winerror = 1314
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(Path, "symlink_to", side_effect=denied):
            profile = m.profile_dir("fallback")
            profile.mkdir(parents=True)
            done = m.repair_links(profile, True, "fallback")
            self.assertFalse([error for _, _, error in done if error])
            self.assertTrue(m.is_junction(profile / "projects"))
            self.assert_shared(profile / "settings.json")
            self.assertIn("settings.json", m.read_json(profile / ".ccm-hardlinks.json"))
            m.write_json(self.settings, {"model": "changed"})
            self.assertFalse((profile / "settings.json").samefile(self.settings))
            m.repair_links(profile, True, "fallback")
            self.assert_shared(profile / "settings.json")
            m.remove_profile_dir(profile)
        self.assertEqual(json.loads(self.settings.read_text())["model"], "changed")
        self.assertTrue((self.claude_home / "projects" / "p1" / "s.jsonl").exists())


class HelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_display_width(self):
        self.assertEqual(self.m.dwidth("한글a"), 5)
        self.assertEqual(self.m.dwidth("\x1b[31m한\x1b[0m"), 2)
        self.assertEqual(self.m.truncate("abcdefgh", 5), "abcd…")
        self.assertEqual(self.m.dwidth(self.m.truncate("가나다라마", 6)), 5)

    def test_extract_windows_ignores_malformed_values(self):
        payload = {"rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 100},
                                   "seven_day": {"used_percentage": "x"}, "other": 3}}
        self.assertEqual(self.m.extract_windows(payload),
                         {"five_hour": {"used_percentage": 5.0, "resets_at": 100.0}})
        self.assertEqual(self.m.extract_windows({"rate_limits": None}), {})
        self.assertEqual(self.m.extract_windows([]), {})

    def test_tr_follows_language(self):
        old = self.m._LANG
        try:
            self.m._LANG = "en"
            self.assertEqual(self.m.fmt_ago(0, 7200), "2h ago")
            self.m._LANG = "ko"
            self.assertEqual(self.m.fmt_ago(0, 7200), "2시간 전")
        finally:
            self.m._LANG = old

    def test_strip_rc_block(self):
        block = self.m.shell_function()
        self.assertEqual(self.m.strip_rc_block("a\n\n" + block + "b\n"), "a\nb\n")
        self.assertEqual(self.m.strip_rc_block("a\n"), "a\n")


if __name__ == "__main__":
    unittest.main()
