# codex-quota-resume

> Codex（ChatGPT 桌面版）Plus/Pro 额度用尽后，自动等到额度重置并向当前对话发送「继续」续跑。
>
> 一个 macOS 上的自动化 Skill / 独立脚本：不靠截图 OCR，直接读 Codex 的会话日志拿到**精确重置时间**，到点用 UI 注入把「继续」送进正在卡住的那个对话。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [工作原理](#工作原理)
- [仓库结构](#仓库结构)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [输出语义（三态判定）](#输出语义三态判定)
- [状态机与防重复](#状态机与防重复)
- [定时任务](#定时任务)
- [排错速查](#排错速查)
- [已知限制](#已知限制)
- [移植到其他机器](#移植到其他机器)
- [License](#license)

---

## 它解决什么问题

你让 Codex 干一个长任务，人走开了，回来发现它停在：

```
You've reached your usage limit. Try again at 3:45 PM.
```

Codex 桌面版不会自己排队恢复，任务就这么搁着。本脚本做的事：

1. 从会话日志里读出**精确的** `resets_at`（不是猜的 5 小时，是服务端给的时间戳）；
2. 到点后激活 Codex 窗口、把「继续」粘进输入框、回车；
3. 回读会话日志**验证**是否真的推动了任务，而不是只看「好像发出去了」。

全程可无人值守，自带 `caffeinate` 防休眠、状态记账防重复发送。

## 工作原理

Codex 会把每次请求的额度快照写进会话日志：

```
~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ISO时间>-<thread-uuid>.jsonl
```

每行一个 JSON，其中的 `rate_limits` 字段长这样：

```json
"rate_limits":{"limit_id":"codex","primary":{"used_percent":98.0,"window_minutes":300,"resets_at":1789285007},
 "secondary":{"used_percent":39.0,"window_minutes":10080,"resets_at":1789805781},
 "plan_type":"plus","rate_limit_reached_type":null}
```

| 字段 | 含义 |
|---|---|
| `primary` | 5 小时滚动窗口（Plus 卡的就是它），`resets_at` 为 Unix 秒 |
| `secondary` | 周额度 |
| `used_percent` | 已用百分比，触发续跑前要求 ≥ 85 |
| `rate_limit_reached_type` | 命名额度类型的限流标记 |

脚本取**最新修改**的 rollout 文件的最后一次 `rate_limits` 快照，因此重置时间是**精确值**，比轮询截图/OCR 靠谱得多。

## 仓库结构

```
.
├── SKILL.md                  # Skill 定义（给 WorkBuddy / Hermes 等宿主读取）
├── codex_resume_watch.py     # 主程序：读额度 → 判定 → 注入续跑 → 回读验证
├── preflight.sh              # 环境自检：App 在跑吗？权限够吗？额度还剩多少？
└── README.md
```

运行时会自动生成（已在 `.gitignore` 中忽略）：

```
watch.log        # 每轮日志
state.json       # 状态记账：窗口 / 触发次数 / 是否已验证
cli_resume.out   # CLI 兜底通道的输出
```

## 环境要求

- **macOS**（依赖 `osascript` / `pbcopy` / `caffeinate` / `pkill`）
- **Codex 桌面版**（`/Applications/ChatGPT.app`，bundle id `com.openai.codex`），且目标对话窗口保持打开
- **Python 3.9+**（只用标准库；作者环境为 3.13）
- **辅助功能权限**（UI 注入必需，见下）

### 权限设置（最容易踩的坑）

系统设置 → 隐私与安全性 → 辅助功能 → 勾选 / 重新添加**你的宿主程序**（WorkBuddy，或终端 iTerm/Terminal）。

> ⚠️ 授权后必须 **⌘Q 彻底退出并重开宿主程序**，权限才生效。
> TCC 对「已运行进程」不生效——授权那一刻正在跑的进程拿不到新权限，表现为开关明明是开的、`osascript` 却继续报 `-10004 权限违例`。排查时不要怀疑授权本身，先重启宿主进程。

三条自检命令全过才算通：

```bash
osascript -e 'tell application "System Events" to get name of first process'                            # 进程枚举
osascript -e 'tell application "System Events" to tell process "ChatGPT" to get name of every window'  # 窗口枚举
osascript -e 'tell application "System Events" to keystroke "x"'                                        # 按键注入
```

## 安装

**方式一：作为 Skill 使用**（放在宿主的 skills 目录里，例如 WorkBuddy / Hermes）：

```bash
git clone https://github.com/wd-moo/codex-quota-resume.git ~/.workbuddy/skills/codex-quota-resume
chmod +x ~/.workbuddy/skills/codex-quota-resume/{preflight.sh,codex_resume_watch.py}
```

**方式二：当普通脚本用**：

```bash
git clone https://github.com/wd-moo/codex-quota-resume.git
cd codex-quota-resume
python3 codex_resume_watch.py --no-act
```

> 脚本里有两处硬编码路径（`CODEX_CLI`、`preflight.sh` 里的 `PY`）指向作者的机器，换机器务必先改，见[移植到其他机器](#移植到其他机器)。

## 快速开始

```bash
DIR=~/.workbuddy/skills/codex-quota-resume
PY=python3    # 作者环境：/Users/int/.workbuddy/binaries/python/versions/3.13.12/bin/python3

zsh $DIR/preflight.sh                      # 环境自检：App 在跑？权限够？额度剩多少？
$PY $DIR/codex_resume_watch.py --no-act    # 只看状态，绝不动手（推荐先跑这个）
$PY $DIR/codex_resume_watch.py             # 到点则续跑
$PY $DIR/codex_resume_watch.py --force     # 忽略重置时间，立刻续跑一次（应急）
$PY $DIR/codex_resume_watch.py --test      # 测试模式，打印 CLI 输出尾部便于定位
```

| 参数 | 作用 |
|---|---|
| `--no-act` | 只读模式，只报告额度状态，永不注入 |
| `--force` | 跳过 `now >= resets_at` 判定，立即续跑一次 |
| `--test` | 打印 `cli_resume.out` 尾部，便于肉眼定位 CLI 通道问题 |
| `--allow-app-quit` | 允许先退出 Codex 再用 CLI 兜底（**激进，默认关闭**，等价于环境变量 `CODEX_WATCH_ALLOW_QUIT=1`） |

每轮任务会自行拉起 `caffeinate -i -m -t 3900`（约 65 分钟）防止 Mac 休眠。

## 输出语义（三态判定）

回读日志时**不能只看「文件变大了」**——发了「继续」本身就会让日志变大，哪怕 Codex 立刻回一句「额度不足」。脚本读本轮 `task_complete.error` 来区分：

| 输出 | 含义 | 状态记账 |
|---|---|---|
| `DONE｜✅` | 会话有新增事件 **且** 本轮无额度错误 → 任务真的在推进 | `verified=true`，该窗口收工 |
| `DELIVERED｜♦️` | 「继续」已送达被接收，但 Codex 回 `usage limit` → **通道正常，额度没回来** | `verified=false`，45 分钟后重试 |
| `DONE｜⚠️` | 120 秒内会话毫无变化 → 可能焦点不在输入框 / 没送达 | `verified=false`，45 分钟后重试 |
| `FAIL｜` / `BLOCKED｜⛔` | UI 注入本身失败（权限） | 报警，需人工修 |

> 关键：`DELIVERED` 是**好消息**，说明通道打通了；它和 `DONE｜✅` 的区别只是额度还没回来。

端到端验证方法：注入后去会话日志里 grep 用户消息，看到 `"role": "user"` + `"text": "继续\n"` 就说明真的送达了。送达 + Codex 回 `try again at X:XX PM` = **通道确认可用**。

## 状态机与防重复

`state.json` 里的 `window_fires` 按 `resets_at` 分窗口记账：

- 该窗口**已验证成功** → 这一轮彻底不再动（`verified=true`）；
- 该窗口**发过但没验证成功**（含 `DELIVERED`）→ 冷却 `RETRY_COOLDOWN`（45 分钟）后允许重试一次，用来兜「发了但其实额度还没回来」的情况；
- `fires` 数组做 24 小时滑动上限（`MAX_FIRES_PER_DAY=8`）；
- 触发前提：`now >= resets_at` **且** 最后一次采样 `used_percent >= 85`——后者防止任务其实已经收工、脚本却无限刷「继续」。

UI 注入是主路径，**不要**因为「上轮未验证」就切去 CLI（原因见下）。只在 UI 注入本身报错时才跑一次 CLI 作为诊断。

## 定时任务

在支持定时任务的宿主里（WorkBuddy / Hermes 的 `automation_update`、或系统 `launchd` / `cron`）：

- **常规轮询**：`scheduleType=recurring`，`rrule="FREQ=HOURLY;INTERVAL=1"`；
- **精准补跑**：按 `resets_at + 2 分钟` 再建一条 `scheduleType=once`，避免整点间隔空等；
- 两条并存是安全的，脚本用 `fired_resets_at` 做了同窗口去重。

`BUFFER_SEC = 120` 会让脚本在重置时间过后再等 2 分钟才动手，避开边界抖动。

## 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `-10004 权限违例` | 辅助功能未对宿主程序生效 | ① 用「−」删掉宿主再重新添加；② **⌘Q 退出并重开宿主**（关键） |
| 开关明明开着却仍 `-10004` | 授权后没重启宿主进程 | 重开宿主即可，别怀疑授权本身 |
| 输出 `BLOCKED｜⛔` | UI 注入被系统拒绝 | 按上面两步修权限 |
| 输出 `DELIVERED｜♦️` | 送达成功但 Codex 回 `usage limit` | **正常**，通道已通、额度还没回来；45 分钟后自动重试 |
| `thread-store conflict / active writer` | Codex 桌面版持有会话锁 | 正常现象（App 开着就必然如此），不是 bug |
| `unexpected argument '-s'` | 给 `exec resume` 传了 `-s` | 去掉 `-s`，沙箱走 `config.toml` 的 `default_permissions` |
| 脚本说「没被限流，不动手」 | 最后采样 `used_percent < 85` | 确认是否真被限流；必要时 `--force` |
| `DONE｜⚠️ 已发送但没变化` | 焦点不在输入框 / 消息没送达 | 45 分钟后自动重试；连续出现则检查 Codex 窗口是否被最小化 |
| 整点任务没跑 | Mac 休眠或宿主退出 | 检查 `pgrep -x caffeinate`，保持 App 常驻 |
| 找不到会话 | 目标对话太久没动（>72h） | 调 `CANDIDATE_WINDOW`，或先在 App 里发一条消息激活 |

## 已知限制

### 两条续跑通道

**1. UI 注入 —— 唯一真正可用的通道**（已实测打通）
`osascript` 激活 App → `pbcopy` 写入「继续」→ Cmd+V → `key code 36`。必须授予辅助功能权限，否则报 `-10004`。
该报错与 Bash 沙箱**无关**，加 `dangerouslyDisableSandbox` 也一样——TCC 看的是「责任进程」，必须给宿主程序本体授权。

**2. CLI 兜底 —— 基本没用，别指望它**

```bash
codex exec resume <THREAD_ID> "继续" --skip-git-repo-check
```

- **不要加 `-s` / `--sandbox`**：`codex exec resume` 不接受这两个参数，会直接 `error: unexpected argument '-s' found`。
- **致命限制**：只要 Codex 桌面版还开着这个会话，它就持有 `~/.codex/thread-writer-locks/<thread>.lock`，CLI 必然失败：

  ```
  ERROR thread-store conflict: thread <id> already has an active writer (code -32600)
  ```

  也就是说**桌面版运行时 CLI 兜底 100% 不可用**，两者互斥。只有在 Codex App 完全退出、锁被释放后 CLI 才有意义。
- 实测细节：即使 CLI 因抢锁失败，会话日志里仍可能留下 `task_started` + `turn_aborted`（reason=interrupted）的痕迹，回读校验时要小心别误判成「续跑成功」。

### 已探明但未采用的路

- **`codex://` 深链接**：ChatGPT.app 的 Info.plist 注册了 `codex` scheme，但在 `app.asar` 里 grep 不到任何 `codex://<route>` 字符串，无法构造出能发消息的链接。
- **`~/.codex/ipc/ipc.sock`**：确实存在且被 ChatGPT 进程持有，但协议未公开，逆向成本高。
- **`~/.codex/queue_1.sqlite` 的 `queued_items`**：表结构已知（`id, thread_id, payload_json, queue_order, created_at_ms, updated_at_ms` + 修订号触发器），疑似 App 自带的消息队列，插一行理论上能让 App 自己发消息（免权限、不抢焦点）。但 `payload_json` 的确切 schema 未知（需逆向 `Contents/Resources/codex`，220MB Rust 二进制），且表通常是空的、拿不到样本。**若 UI 注入长期不可用，这是最值得投入的替代方案。**

### 会话日志里用户消息有两种写法（脚本都解析）

```json
{"type":"event_msg","payload":{"type":"user_message","message":"继续"}}                          // 形态一
{"type":"response_item","payload":{"type":"message","role":"user",
 "content":[{"type":"input_text","text":"继续\n"}]}}                                            // 形态二（当前桌面版实际写法）
```

只认形态一会导致「最后用户消息」永远是空的、校验力变弱。

## 移植到其他机器

仓库里的脚本保留了作者环境的绝对路径，换机器请改：

| 位置 | 默认值 | 改成 |
|---|---|---|
| `codex_resume_watch.py` → `CODEX_CLI` | `/Users/int/.local/bin/codex` | 你的 `which codex` 结果 |
| `preflight.sh` → `PY` | `/Users/int/.workbuddy/binaries/python/versions/3.13.12/bin/python3` | 你的 `which python3` 结果 |

其余路径（`~/.codex/...`、日志与状态文件）都是相对/家目录推导的，无需改动。

## License

MIT
