"""ccm 동작 테스트.

실제 키체인과 ~/.claude는 건드리지 않는다. 프로젝트의 _workspace/tmp 아래에
임시 폴더를 만들고 가짜 claude(tests/fake_claude.py)로 로그인 상태를 흉내 낸다.
"""

import fcntl
import importlib.machinery
import importlib.util
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

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
        (self.claude_home / "CLAUDE.md").write_text("# 지침\n")
        self.settings = self.claude_home / "settings.json"
        self.settings.write_text(json.dumps(
            {"model": "opus", "statusLine": {"type": "command", "command": "printf base", "padding": 0}},
            indent=2) + "\n")
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        fake.chmod(0o755)
        self.log = self.root / "claude.log"
        drop = {"CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN", "CCM_SHELL_HOOK", "CLAUDECODE"}
        self.env = {k: v for k, v in os.environ.items() if k not in drop}
        self.env.update(
            CCM_HOME=str(self.root / "ccm"),
            CCM_CLAUDE_HOME=str(self.claude_home),
            CCM_CLAUDE_BIN=str(fake),
            HOME=str(self.home),
            SHELL="/bin/zsh",
            FAKE_CLAUDE_LOG=str(self.log),
            FAKE_DEFAULT_DIR=str(self.claude_home),
            NO_COLOR="1",
            COLUMNS="140",
            PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------- 도우미 ----------

    def ccm(self, *args, env=None, input=None, code=0):
        r = subprocess.run([sys.executable, str(CCM), *args], env=env or self.env, input=input,
                           capture_output=True, text=True, timeout=60)
        if code is not None and r.returncode != code:
            self.fail(f"ccm {' '.join(args)} → {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        return r

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def pdir(self, name):
        return self.root / "ccm" / "profiles" / name

    def config(self):
        return json.loads((self.root / "ccm" / "config.json").read_text())

    def write_usage(self, name, windows, updated_at=None):
        path = self.root / "ccm" / "usage" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"windows": windows, "updated_at": updated_at or time.time()}))

    # ---------- 프로필 추가 ----------

    def test_add_links_shared_items_and_logs_in(self):
        r = self.ccm("add", "work", "--email", "w@example.com")
        d = self.pdir("work")
        for item in ("settings.json", "CLAUDE.md", "skills", "projects"):
            self.assertTrue((d / item).is_symlink(), item)
            self.assertEqual((d / item).resolve(), (self.claude_home / item).resolve())
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
            self.assertTrue((self.pdir("work") / item).is_symlink(), item)

    def test_split_state_is_moved_back_and_linked(self):
        self.ccm("add", "work")
        d, shared = self.pdir("work"), self.claude_home / "tasks"
        # Claude Code가 계정 폴더 안에 tasks를 따로 만든 상황을 흉내 낸다
        (d / "tasks").unlink()
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
        self.assertTrue((d / "tasks").is_symlink())
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
        (d / "sessions").unlink()
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
        self.assertTrue((d / "settings.json").is_symlink())
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
        self.assertIn("unset ANTHROPIC_API_KEY", out)

        # Claude Code 세션 안에서 실행하면 세션에서 물려받은 값이라고 안내한다
        out = self.ccm("ls", env={**self.env, "ANTHROPIC_API_KEY": "x", "CLAUDECODE": "1"}).stdout
        self.assertIn("Claude Code 세션 안에서 실행해서", out)
        self.assertNotIn("unset", out)

    def test_ls_warns_when_statusline_is_not_connected(self):
        self.assertIn("ccm setup", self.ccm("ls").stdout)
        self.ccm("setup", "--no-shell")
        self.assertNotIn("상태줄이 연결되어 있지 않아", self.ccm("ls").stdout)

    # ---------- 상태줄 ----------

    def test_statusline_records_usage_and_keeps_previous_output(self):
        self.ccm("add", "work")
        self.ccm("setup", "--no-shell")
        settings = json.loads(self.settings.read_text())
        command = settings["statusLine"]["command"]
        self.assertTrue(command.endswith(" statusline"))
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
                           capture_output=True, text=True, timeout=30)
        self.assertTrue(r.stdout.startswith("base  [work] 5h 72% ↻"), r.stdout)
        self.assertIn("· 7d 31% ↻", r.stdout)
        rec = json.loads((self.root / "ccm" / "usage" / "work.json").read_text())
        self.assertEqual(rec["windows"]["five_hour"]["used_percentage"], 72.4)

        # rate_limits가 없는 호출(세션 첫 응답 전)은 마지막 기록을 보여 주고 기록은 바꾸지 않는다
        r = subprocess.run(command, shell=True, input="{}", env=env, capture_output=True, text=True)
        self.assertIn("[work] 5h 72%", r.stdout)

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
        rc = self.home / ".zshrc"

        def run_claude(extra_env=None):
            env = {**self.env, **(extra_env or {})}
            subprocess.run(["zsh", "-f", "-c", f"source {rc}; claude hello"], env=env, check=True, timeout=30)
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

    def test_pick_enter_confirms_exit(self):
        s = PtySession([], self.env)
        s.expect("ESC 종료")
        s.send(b"\x1b")
        s.expect("종료할까요?")
        s.send(b"\r")
        s.expect("종료했습니다.")
        s.close()
        self.assertEqual(s.p.returncode, 0)

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
        rc = self.home / ".zshrc"
        rc.write_text("export FOO=1\n")
        original_settings = json.loads(self.settings.read_text())
        self.ccm("setup")
        r = self.ccm("setup")
        self.assertIn("이미 연결", r.stdout)
        self.assertIn("이미 설치", r.stdout)
        self.assertEqual(rc.read_text().count("# >>> ccm >>>"), 1)
        self.ccm("setup", "--undo")
        self.assertEqual(json.loads(self.settings.read_text()), original_settings)
        self.assertEqual(rc.read_text(), "export FOO=1\n")

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
        self.assertTrue((d / "CLAUDE.md").is_symlink())


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

    def test_strip_rc_block(self):
        block = self.m.shell_function()
        self.assertEqual(self.m.strip_rc_block("a\n\n" + block + "b\n"), "a\nb\n")
        self.assertEqual(self.m.strip_rc_block("a\n"), "a\n")


if __name__ == "__main__":
    unittest.main()
