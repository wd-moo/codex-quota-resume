---
name: codex-quota-resume
description: 当 Codex（ChatGPT 桌面版）Plus/Pro 的 5 小时额度用尽、任务卡在 "usage limit reached" 时，读取真实重置时间并在额度恢复后自动往当前对话发送「继续」续跑。触发词：Codex 没额度了、Codex 额度用光、usage limit、额度重置后续跑、让 Codex 自己继续、Codex 卡住了、codex out of usage、自动给 Codex 发继续。
agent_created: true
---

# Codex 额度重置自动续跑

## 适用场景
用户外出/离开，Codex 桌面版（`/Applications/ChatGPT.app`，bundle id `com.openai.codex`）因额度耗尽停在
"You've reached your usage limit"，需要一个定时任务在额度恢复后自动发「继续」。

## 核心原理（别用截图 OCR）

Codex 会把每次请求的额度状态写进会话日志，直接读文件即可拿到**精确**的重置时刻：

```
~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ISO时间>-<thread-uuid>.jsonl
```

每行一个 JSON，其中会出现：

```json
"rate_limits":{"limit_id":"codex","primary":{"used_percent":98.0,"window_minutes":300,"resets_at":1789285007},
 "secondary":{"used_percent":39.0,"window_minutes":10080,"resets_at":1789805781},
 "plan_type":"plus","rate_limit_reached_type":null}
```

- `primary` = 5 小时滚动窗口（Plus 卡的就是它），`resets_at` 是 Unix 秒。
- `secondary` = 周额度。
- 取**最新修改**的那个 rollout 文件的最后一次 `rate_limits` 快照即可。

## 用法

```bash
DIR=~/.workbuddy/skills/codex-quota-resume
PY=/Users/int/.workbuddy/binaries/python/versions/3.13.12/bin/python3

zsh $DIR/preflight.sh                 # 环境自检：App 在跑？权限够？额度还剩多少？
$PY $DIR/codex_resume_watch.py --no-act   # 只看状态，绝不动手
$PY $DIR/codex_resume_watch.py            # 到点则续跑
$PY $DIR/codex_resume_watch.py --force     # 忽略重置时间，立刻续跑一次（应急）
```

脚本会自己写 `watch.log` / `state.json`，并拉起 `caffeinate -i -m -t 3900` 防休眠。

## 两条续跑通道（2026-09-13 实测结论，重要）

### 1. UI 注入 —— **唯一真正可用的通道（2026-09-13 已实测打通），且必需辅助功能权限**
`osascript` 激活 App → `pbcopy` 写入「继续」→ Cmd+V → `key code 36`（回车）。

- 必须授予「辅助功能」权限，否则报 `-10004 权限违例`。
- 授权路径：系统设置 → 隐私与安全性 → 辅助功能 → 勾选 / 重新添加 **WorkBuddy**。
- **⚠️ 授权后必须 ⌘Q 彻底退出并重开 WorkBuddy，权限才生效。**
  TCC 是对「已运行进程」不生效的——授权那一刻正在跑的进程拿不到新权限，
  表现为开关明明是开的、`osascript` 却继续报 `-10004`。这是最容易踩的坑，
  排查时不要怀疑授权本身，先重启宿主进程。
- 自检（三条全过才算通）：
  ```bash
  osascript -e 'tell application "System Events" to get name of first process'                      # 进程枚举
  osascript -e 'tell application "System Events" to tell process "ChatGPT" to get name of every window'  # 窗口枚举
  osascript -e 'tell application "System Events" to keystroke "x"'                                   # 按键注入
  ```
- 注意：这个报错跟 Bash 沙箱**无关**，加 `dangerouslyDisableSandbox` 也一样。
  TCC 看的不是沙箱，而是「责任进程」，所以必须给 WorkBuddy 本体授权。
- **端到端验证方法**：注入后去会话日志里 grep 用户消息，能看到
  `"role": "user"` + `"text": "继续\n"` 就说明真的送达了。
  送达 + Codex 回 `You've hit your usage limit... try again at X:XX PM` = **通道确认可用**。

### 2. CLI 兜底 —— **基本没用，别指望它**
```bash
codex exec resume <THREAD_ID> "继续" --skip-git-repo-check
```
- **不要加 `-s` / `--sandbox`**：`codex exec resume` 不接受这两个参数，会直接
  `error: unexpected argument '-s' found`。沙箱策略走 `~/.codex/config.toml` 的 `default_permissions`。
- **致命限制**：只要 Codex 桌面版还开着这个会话，它就持有
  `~/.codex/thread-writer-locks/<thread>.lock`，CLI 必然失败：
  ```
  ERROR thread-store conflict: thread <id> already has an active writer (code -32600)
  ```
  也就是说，**桌面版运行时 CLI 兜底 100% 不可用**。两者互斥。
- 只有在 Codex App 完全退出、锁被释放后 CLI 才有意义。脚本里的
  `--allow-app-quit`（或环境变量 `CODEX_WATCH_ALLOW_QUIT=1`）会先退 App 再走 CLI，
  属于**激进手段，默认关闭**，启用前必须问过用户——因为用户回来后 App 是关着的，
  而且 headless 跑可能会卡在审批上。
- 实测细节：即使 CLI 因抢锁失败，会话日志里仍可能留下 `task_started` + `turn_aborted`
  （reason=interrupted）的痕迹，回读校验时要小心把它误判成「续跑成功」。

## 输出语义（三态判定，**别混淆**）
回读日志时不能只看「文件变大了」——发了「继续」本身就会让日志变大，哪怕 Codex 立刻回
"额度不足"。所以脚本读本轮 `task_complete.error` 来区分：

| 输出 | 含义 | 状态记账 |
|---|---|---|
| `DONE｜✅` | 会话有新增事件 **且** 本轮无额度错误 → 任务真的在推进 | `verified=true`，该窗口收工 |
| `DELIVERED｜♦️` | 「继续」已送达被接收，但 Codex 回 `usage limit` → **通道正常，额度没回来** | `verified=false`，45 分钟后重试 |
| `DONE｜⚠️` | 120 秒内会话毫无变化 → 可能焦点不在输入框 / 没送达 | `verified=false`，45 分钟后重试 |
| `FAIL｜` / `BLOCKED｜⛔` | UI 注入本身失败（权限） | 报警，需人工修 |

> 关键：`DELIVERED` 是**好消息**，说明通道打通了；它和 `DONE｜✅` 的区别是额度还没回来。

## 状态机（防重复 / 防漏跑）
`state.json` 里 `window_fires` 按 `resets_at` 分窗口记账：
- 该窗口**已验证成功** → 这一轮彻底不再动（`verified=true`）。
- 该窗口**发过但没验证成功**（含 `DELIVERED`）→ 冷却 `RETRY_COOLDOWN`（45 分钟）后允许重试一次，
  用来兜「发了但其实额度还没回来」的情况。
- `fires` 数组做 24 小时滑动上限（`MAX_FIRES_PER_DAY=8`）。
- 触发前提：`now >= resets_at` **且** 最后一次采样 `used_percent >= 85`
  —— 后者防止任务其实已经收工、我们却无限刷「继续」。
- UI 通道是主路径，**不要**因为「上轮未验证」就切去 CLI——CLI 在桌面版开着会话时
  必然 `active-writer` 冲突。只在 UI 注入本身报错时才跑一次 CLI 作为诊断。

## 会话日志里的用户消息有**两种**写法（都解析）
```json
{"type":"event_msg","payload":{"type":"user_message","message":"继续"}}            // 形态一
{"type":"response_item","payload":{"type":"message","role":"user",
 "content":[{"type":"input_text","text":"继续\n"}]}}                              // 形态二（当前桌面版实际写法）
```
只认形态一会导致「最后用户消息」永远是空的、校验力变弱。脚本两种都读。
本轮结束时 Codex 的报错在 `event_msg` → `payload.type == "task_complete"` → `payload.error.message`。

## 已探明但未采用的通道
`~/.codex/queue_1.sqlite` 里有 `queued_items(thread_id, payload_json, queue_order)` + `queued_thread_revisions` 触发器，
像是 App 自带的消息队列，理论上插一行就能让 App 自己发消息（免权限、不抢焦点）。
但 `payload_json` 的确切 schema 未知，需逆向 `Contents/Resources/codex`（220MB Rust 二进制）。
如果哪天 UI 注入长期不可用，值得花时间逆向这条路。

### 更正（2026-10）：`codex://threads/<id>` 深链**可导航**、不能发消息
早先「asar 里 grep 不到 `codex://<route>`」的结论不对——普通 `grep` 会被 minify 超长行挡住，
用 `grep -aoE`（或 Python 按字节扫）能找到 10 处 `codex://threads` 路由引用。
实测 `open "codex://threads/<uuid>"` 能把 App 前台窗口精确切到该对话（仅导航，不代发消息）。
所以多会话场景的正确做法：**深链导航到目标 thread → 再走 fire_ui 的 UI 注入**，
就能给任意指定 thread 续跑，而不是只能等它恰好是「mtime 最新」的那个。

## 排错速查
| 现象 | 原因 | 处理 |
|---|---|---|
| `-10004 权限违例` | 辅助功能未对 WorkBuddy 生效 | ① 辅助功能里用「−」删掉 WorkBuddy 再重新添加；② **⌘Q 退出并重开 WorkBuddy**（关键！TCC 对已运行进程不生效） |
| 开关明明开着却仍 `-10004` | 授权后没重启宿主进程 | 重开 WorkBuddy 即可，别怀疑授权本身 |
| 输出 `BLOCKED｜⛔` | UI 注入被系统拒绝 | 按上面两步修权限 |
| 输出 `DELIVERED｜♦️` | 送达成功但 Codex 回 `usage limit` | **正常**，通道已通、额度还没回来；45 分钟后自动重试 |
| `thread-store conflict / active writer` | Codex 桌面版持有会话锁 | 正常现象（App 开着就必然如此），不是 bug |
| `unexpected argument '-s'` | 给 `exec resume` 传了 `-s` | 去掉 `-s`，用 `config.toml` 的 `default_permissions` |
| 脚本说「没被限流，不动手」 | 最后采样 used_percent < 85 | 确认用户是否真被限流；必要时 `--force` |
| `DONE｜⚠️ 已发送但没变化` | 焦点不在输入框 / 消息没送达 | 45 分钟后自动重试；连续出现则检查 Codex 窗口是否被最小化 |
| 整点任务没跑 | Mac 休眠或 WorkBuddy 退出 | 检查 `pgrep -x caffeinate`，保持 App 常驻 |
| 找不到会话 | 目标对话太久没动（>72h） | 调 `CANDIDATE_WINDOW`，或让用户在 App 里发一条消息 |

## 已知没走通的路（别再重复踩）
- **`codex://` 深链接发消息**：走不通（见下方 2026-10 更正——但**导航/切会话可用**）。
  ChatGPT.app 的 Info.plist 注册了 `codex` scheme，`app.asar` 里也没有
  `codex://threads/<id>?prompt=<text>` 这类「带参发消息」的处理器——
  deep link 只做路由导航，不能代发消息。发消息仍需走 UI 注入。
- **`~/.codex/ipc/ipc.sock`**：确实存在且被 ChatGPT 进程持有，但协议未公开，逆向成本高。
- **`~/.codex/queue_1.sqlite` 的 `queued_items`**：表结构已知
  （`id, thread_id, payload_json, queue_order, created_at_ms, updated_at_ms` + 修订号触发器），
  疑似 App 的消息队列，插一行理论上能让 App 自己发消息（免权限、不抢焦点）。
  但 `payload_json` 的确切 schema 未知（需逆向 `Contents/Resources/codex`，220MB Rust 二进制），
  且表通常是空的、拿不到样本。**如果 UI 注入长期不可用，这是最值得投入的替代方案。**

## 定时任务怎么建
用 `automation_update`：
- 常规：`scheduleType=recurring`，`rrule="FREQ=HOURLY;INTERVAL=1"`。
- 精准补跑：按 `resets_at + 2 分钟` 再建一条 `scheduleType=once`，避免整点间隔空等。
- 两条并存是安全的，脚本用 `fired_resets_at` 做了同窗口去重。
