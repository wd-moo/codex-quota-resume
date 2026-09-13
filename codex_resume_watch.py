#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Codex 额度重置守卫 (Codex Resume Watcher)

用途
----
Codex（ChatGPT.app，bundle id com.openai.codex）Plus 版的 5 小时额度用光后，
正在跑的任务会停在 "usage limit reached"。本脚本：

  1. 从 ~/.codex/sessions/**/*.jsonl 读取「真实」的额度状态
     （primary.used_percent / primary.resets_at），不靠截图 OCR；
  2. 到达重置时间后，把光标送进 Codex 窗口：
       优先 UI 注入：激活 App -> 剪贴板写入「继续」-> Cmd+V -> 回车
       降级 CLI 续跑：codex exec resume <thread_id> "继续"
  3. 写日志 + 状态，防止重复触发。

用法
----
  python3 codex_resume_watch.py            # 检查，条件满足则续跑
  python3 codex_resume_watch.py --no-act   # 只检查，绝不动作（安全模式）
  python3 codex_resume_watch.py --force    # 忽略重置时间，立即续跑一次（手动应急）
"""

import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------- 配置
CODEX_HOME = os.path.expanduser("~/.codex")
SESS_GLOB = os.path.join(CODEX_HOME, "sessions", "*", "*", "*", "*.jsonl")
CODEX_CLI = "/Users/int/.local/bin/codex"
APP_ID = "com.openai.codex"

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(WORK_DIR, "watch.log")
STATE_FILE = os.path.join(WORK_DIR, "state.json")
CLI_OUT = os.path.join(WORK_DIR, "cli_resume.out")

RESUME_TEXT = "继续"
BUFFER_SEC = 120          # 重置时间过后再多等 2 分钟，避开边界抖动
CANDIDATE_WINDOW = 72 * 3600   # 只在最近 3 天动过的会话里找目标
MAX_FIRES_PER_DAY = 8
RETRY_COOLDOWN = 45 * 60   # 同一窗口「发了但没验证成功」时，多久后允许重试
VERIFY_WAIT = 120         # 触发后等 120 秒回读日志，确认真的动起来了

# 会话里最后只用了 90% 以上的额度才算「被限流」，避免误触
EXHAUSTED_PCT = 85.0


def now_ts():
    return time.time()


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%F %T"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("fires", [])
    st.setdefault("window_fires", {})
    st.setdefault("ui_unverified", False)
    st.setdefault("last_target", None)
    return st


def save_state(st):
    cutoff = now_ts() - 86400
    st["fires"] = [t for t in st.get("fires", []) if t > cutoff]
    # 只保留最近 20 轮额度窗口的记录，别让状态文件无限膨胀
    wf = st.get("window_fires", {})
    if len(wf) > 20:
        keep = sorted(wf.items(), key=lambda kv: kv[1].get("last_ts", 0))[-20:]
        st["window_fires"] = dict(keep)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("! 状态写入失败: %s" % e)


def window_gate(st, resets_at):
    """判断这一轮额度窗口该不该动手。

    规则（比「同一窗口只发一次」更耐操）：
      · 已经验证续上过 -> 这一轮彻底不再动
      · 发过但没验证成功 -> 冷却 RETRY_COOLDOWN 秒后允许重试（应对「发了但额度其实还没回来」）
    """
    wf = st.get("window_fires", {}).get(str(resets_at))
    if not wf:
        return True, "本轮窗口还没发过"
    if wf.get("verified"):
        return False, "本轮窗口已确认续跑成功（%d 次尝试）" % wf.get("count", 1)
    waited = now_ts() - wf.get("last_ts", 0)
    if waited < RETRY_COOLDOWN:
        return False, "本轮窗口刚发过但未验证成功，冷却中（还剩 %d 分钟）" % (
            (RETRY_COOLDOWN - waited) // 60
        )
    return True, "本轮窗口发过但未验证成功，已过冷却，重试第 %d 次" % (wf.get("count", 1) + 1)


def record_fire(st, resets_at, verified):
    wf = st.setdefault("window_fires", {})
    key = str(resets_at)
    cur = wf.get(key, {"count": 0, "last_ts": 0, "verified": False})
    cur["count"] = cur.get("count", 0) + 1
    cur["last_ts"] = now_ts()
    cur["verified"] = bool(verified) or cur.get("verified", False)
    wf[key] = cur
    st["ui_unverified"] = not bool(verified)
    st.setdefault("fires", []).append(now_ts())
    save_state(st)


# ---------------------------------------------------------------- 读取额度
def find_target():
    """返回最近活跃的会话文件路径（mtime 最新且在 CANDIDATE_WINDOW 内）"""
    best, best_mt = None, 0
    for p in glob.glob(SESS_GLOB):
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt > best_mt:
            best, best_mt = p, mt
    if best is None or now_ts() - best_mt > CANDIDATE_WINDOW:
        return None, 0
    return best, best_mt


def thread_id_of(path):
    m = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        os.path.basename(path),
    )
    return m.group(1) if m else None


RL_RE = re.compile(r'"rate_limits":(\{.*?"rate_limit_reached_type":[^,}]*\})')


def read_session(path):
    """解析会话文件：最后一次额度快照 / 最后一个事件 / 最后一条用户消息"""
    info = {
        "rate": None,
        "last_event_ts": None,
        "last_user_msg": None,
        "last_user_ts": None,
        "last_turn_error": None,
        "size": 0,
    }
    try:
        info["size"] = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                low = line
                if '"rate_limits"' in low:
                    m = RL_RE.search(low)
                    if m:
                        try:
                            o = json.loads(m.group(1))
                            if isinstance(o.get("primary"), dict):
                                info["rate"] = o
                        except Exception:
                            pass
                if '"event_msg"' in low or '"response_item"' in low:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ts = d.get("timestamp")
                    if ts:
                        info["last_event_ts"] = ts
                    p = d.get("payload") or {}
                    ptype = p.get("type")
                    # 形态一：event_msg/user_message
                    if ptype == "user_message":
                        info["last_user_msg"] = str(p.get("message"))[:80]
                        info["last_user_ts"] = ts
                    # 形态二：response_item/message + role=user（当前桌面版实际写法）
                    elif ptype == "message" and p.get("role") == "user":
                        txt = ""
                        for it in (p.get("content") or []):
                            if isinstance(it, dict):
                                txt += str(it.get("text") or it.get("content") or "")
                            else:
                                txt += str(it)
                        txt = txt.strip()
                        if txt:
                            info["last_user_msg"] = txt[:80]
                            info["last_user_ts"] = ts
                    # 记录本轮结束时的错误（用于区分「真续跑」和「额度仍不足」）
                    if ptype == "task_complete":
                        err = p.get("error") or {}
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                        info["last_turn_error"] = msg or None
    except Exception as e:
        log("! 读取会话失败: %s" % e)
    return info


def fmt_ts(epoch):
    return datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 动作
def keep_awake():
    """防止 Mac 空闲休眠，导致整点任务不触发（约 65 分钟）"""
    try:
        out = subprocess.run(["pgrep", "-x", "caffeinate"], capture_output=True, timeout=10)
        if out.returncode == 0:
            return
    except Exception:
        pass
    try:
        subprocess.Popen(
            ["caffeinate", "-i", "-m", "-t", "3900"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log("  · 已启动 caffeinate 防止休眠")
    except Exception as e:
        log("! caffeinate 启动失败: %s" % e)


def run_as(args, **kw):
    return subprocess.run(args, capture_output=True, timeout=30, **kw)


def fire_ui():
    """激活 Codex 窗口 -> 粘贴「继续」-> 回车。返回 (是否成功, 说明)"""
    # 1) 激活 App（不需要辅助功能权限）
    r = run_as(["osascript", "-e", 'tell application id "%s" to activate' % APP_ID])
    if r.returncode != 0:
        return False, "激活 App 失败: %s" % r.stderr.decode(errors="ignore").strip()
    time.sleep(2.0)

    # 2) 写剪贴板
    try:
        run_as(["pbcopy"], input=RESUME_TEXT.encode("utf-8"))
    except Exception as e:
        return False, "写剪贴板失败: %s" % e

    # 3) 粘贴 + 回车（需要「辅助功能」权限）
    r = run_as(
        ["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down']
    )
    if r.returncode != 0:
        err = r.stderr.decode(errors="ignore").strip()
        return False, "按键注入被拒绝（缺辅助功能权限）: %s" % err
    time.sleep(0.8)
    r = run_as(["osascript", "-e", 'tell application "System Events" to key code 36'])
    if r.returncode != 0:
        return False, "回车注入失败: %s" % r.stderr.decode(errors="ignore").strip()
    return True, "UI 注入成功（已粘贴「继续」并回车）"


def dump_cli_out(n=25):
    """测试模式下把 CLI 续跑的输出打出来，方便肉眼定位问题"""
    if not os.path.exists(CLI_OUT):
        log("  · CLI 输出文件还不存在")
        return
    try:
        with open(CLI_OUT, encoding="utf-8", errors="ignore") as f:
            lines = [l.rstrip() for l in f if l.strip()]
        log("  · CLI 输出尾部（共 %d 行）：" % len(lines))
        for l in lines[-n:]:
            print("      | " + l[:180], flush=True)
    except Exception as e:
        log("  · 读取 CLI 输出失败: %s" % e)


def fire_cli(thread_id):
    """兜底：用 codex CLI 直接续跑同一会话。

    实测结论（2026-09-13）：只要 Codex 桌面版还开着这个 thread，它持有
    ~/.codex/thread-writer-locks/<thread>.lock，CLI 必然报
    "thread-store conflict: ... already has an active writer"。
    所以这条通道只在 Codex App 已退出时才有效（配合 --allow-app-quit）。

    注意：`codex exec resume` 不吃 -s/--sandbox，沙箱走 config.toml 的 default_permissions。
    """
    # 先清掉旧输出的尾部，便于判断这一轮的结果
    try:
        with open(CLI_OUT, "w", encoding="utf-8") as f:
            f.write("===== %s CLI 续跑 =====\n" % datetime.now().isoformat())
    except Exception:
        pass

    args = [
        CODEX_CLI, "exec", "resume", thread_id, RESUME_TEXT,
        "--skip-git-repo-check",
    ]
    try:
        fh = open(CLI_OUT, "ab")
        proc = subprocess.Popen(
            args, stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception as e:
        return False, "CLI 续跑启动失败: %s" % e, ""

    # 盯 15 秒：抢锁冲突是秒级失败的，能立刻分辨「真的在跑」还是「被锁挡住」
    deadline = now_ts() + 15
    while now_ts() < deadline:
        time.sleep(1.5)
        if proc.poll() is not None:
            break
    try:
        with open(CLI_OUT, encoding="utf-8", errors="ignore") as f:
            out = f.read()
    except Exception:
        out = ""

    if "active writer" in out:
        return False, "CLI 被会话锁挡住（Codex 桌面版正持有该会话）", out
    if "usage limit" in out.lower() or "rate limit" in out.lower():
        return True, "CLI 已发出「继续」，服务端回的是额度不足（说明通道 OK）", out
    if proc.poll() is None:
        return True, "CLI 续跑正在跑（进程存活）", out
    if proc.returncode == 0:
        return True, "CLI 续跑正常结束", out
    return False, "CLI 退出码 %s" % proc.returncode, out


def quit_codex_app():
    """释放会话锁：退出 Codex 桌面版，好让 CLI 接管"""
    try:
        r = subprocess.run(["osascript", "-e", 'quit app id "%s"' % APP_ID],
                           capture_output=True, timeout=20)
        if r.returncode != 0:
            subprocess.run(["pkill", "-f", "ChatGPT.app/Contents/MacOS/ChatGPT"], timeout=15)
    except Exception:
        try:
            subprocess.run(["pkill", "-f", "ChatGPT.app/Contents/MacOS/ChatGPT"], timeout=15)
        except Exception as e:
            log("! 退出 Codex 失败: %s" % e)
            return False
    for _ in range(20):
        time.sleep(1.5)
        r = subprocess.run(["pgrep", "-f", "ChatGPT.app/Contents/MacOS/ChatGPT"],
                           capture_output=True)
        if r.returncode != 0:
            log("  · Codex 桌面版已退出，会话锁释放")
            return True
    log("! Codex 桌面版 30 秒内没退干净")
    return False


# ---------------------------------------------------------------- 主流程
def main():
    force = "--force" in sys.argv
    no_act = "--no-act" in sys.argv
    test = "--test" in sys.argv
    allow_quit = "--allow-app-quit" in sys.argv or os.environ.get("CODEX_WATCH_ALLOW_QUIT") == "1"
    if force:
        test = True

    keep_awake()

    path, mt = find_target()
    if not path:
        log("WAITING｜找不到最近活跃的 Codex 会话")
        return 0

    tid = thread_id_of(path)
    info = read_session(path)
    rate = info["rate"]

    if not rate:
        log("WAITING｜%s 里没有额度快照（可能该会话还没发过请求）" % os.path.basename(path)[:40])
        return 0

    pr = rate.get("primary") or {}
    pct = float(pr.get("used_percent") or 0)
    resets_at = int(pr.get("resets_at") or 0)
    window_min = pr.get("window_minutes") or 300
    plan = rate.get("plan_type")
    remain = resets_at - now_ts()

    log(
        "CHECK｜目标会话 %s ｜ 额度 %.0f%% ｜ 计划 %s ｜ 窗口 %s 分钟 ｜ 重置 %s ｜ 剩余 %s"
        % (
            (tid or "?")[:8],
            pct,
            plan,
            window_min,
            fmt_ts(resets_at) if resets_at else "?",
            ("%d 分 %d 秒" % (remain // 60, remain % 60)) if remain > 0 else "已过",
        )
    )
    log("  · 会话最后事件 %s" % (info["last_event_ts"] or "?"))

    exhausted = pct >= EXHAUSTED_PCT
    if not test:
        if remain > 0:
            log("WAITING｜额度窗口还没重置，%.0f 分钟后（%s）再试" % (remain / 60.0, fmt_ts(resets_at)))
            return 0
        if not exhausted:
            log("WAITING｜窗口已重置，但续跑前最后一次采样只有 %.0f%%，判断没被限流，不动手" % pct)
            return 0

    st = load_state()
    if not test:
        allowed, gate_why = window_gate(st, resets_at)
        if not allowed:
            log("SKIP｜%s" % gate_why)
            return 0
        if len(st.get("fires", [])) >= MAX_FIRES_PER_DAY:
            log("SKIP｜最近 24 小时已经续跑 %d 次，达到上限，停手" % MAX_FIRES_PER_DAY)
            return 0
        log("  · 窗口判定：%s" % gate_why)

    if no_act:
        log("DRYRUN｜条件已满足，但 --no-act 模式不动作")
        return 0

    log("TEST｜测试模式：真发一次，但不写状态、不影响后续额度窗口" if test else
        "READY｜额度已恢复，开始续跑会话 %s" % (tid or "?")[:8])
    st["last_target"] = tid

    fire_time = now_ts()
    before_size = info["size"]

    # UI 注入是唯一可行通道：CLI 在桌面版开着同一会话时必然 active-writer 冲突（2026-09-13 实测）
    cli_out = ""
    ui_ok, ui_why = fire_ui()
    log("  · UI 路径：%s" % ui_why)

    if ui_ok:
        wait = VERIFY_WAIT
        log("  · 等待 %d 秒回读日志确认…" % wait)
        time.sleep(wait)
        after = read_session(path)
        grew = after["size"] > before_size or (after["last_event_ts"] or "") > (
            info["last_event_ts"] or ""
        )
        err = (after["last_turn_error"] or "")
        quota_blocked = any(
            k in err.lower() for k in ("usage limit", "usage_limit", "rate limit", "quota")
        ) or "usage limit" in err

        if grew and not quota_blocked:
            # 真·续跑成功：会话有新增事件且本轮没有额度报错
            if not test:
                record_fire(st, resets_at, verified=True)
            log(
                "DONE｜✅ 已续跑成功，任务在推进。最后事件 %s ｜ 最后用户消息：%s"
                % (after["last_event_ts"], (after["last_user_msg"] or "")[:40])
            )
        elif grew and quota_blocked:
            # 送达成功、Codex 明确回了额度不足 —— 通道已验证，只是额度还没回来
            if not test:
                record_fire(st, resets_at, verified=False)
            log("DELIVERED｜♦️ 「继续」已送达并被 Codex 接收，但额度仍未恢复：%s" % err[:160])
            log("  · 通道正常。%d 分钟后会自动再试一次，直到真的跑起来" % (RETRY_COOLDOWN // 60))
        else:
            if not test:
                record_fire(st, resets_at, verified=False)
            log(
                "DONE｜⚠️ 信号已发出但 %.0f 秒内会话无变化：可能焦点没落在输入框。"
                "%d 分钟后自动重试" % (wait, RETRY_COOLDOWN // 60)
            )
        if test:
            dump_cli_out()
    else:
        log("FAIL｜UI 注入失败：%s" % ui_why)
        # CLI 仅作诊断（预期 active-writer 冲突），不进主路径
        ok2, why2, cli_out = fire_cli(tid)
        log("  · CLI 诊断路径：%s" % why2)
        if ("辅助功能" in ui_why or "-10004" in ui_why):
            log(
                "BLOCKED｜⛔ UI 注入被系统拒绝（辅助功能权限未生效）。"
                "修复：系统设置 → 隐私与安全性 → 辅助功能 → 把 WorkBuddy 用「−」删掉再重新添加 → "
                "然后 ⌘Q 彻底退出并重开 WorkBuddy（权限对已运行进程不生效）。"
            )
        if test:
            dump_cli_out()
        if not test:
            save_state(st)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
