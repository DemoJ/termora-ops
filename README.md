# termora-ops

让 AI Agent 直接复用本机 [Termora](https://github.com/TermoraDev/termora) 终端里保存的服务器连接信息，自动 SSH 登录执行运维命令、传输文件——**用户不需要再报 IP、账号和密码**。

> 适用场景：你对 Agent 说"上群晖看看磁盘"、"重启一下 k3s-master"、"把日志拉下来"，Agent 自动从 Termora 凭据库匹配主机并连接，全程无需手动输入凭据。

## 工作原理

```
~/.termora/storage/00000000000.xd   (Termora 的 MapDB 凭据存储, JSON 记录)
        │
        ├── termora_hosts.py   只读解析 → 主机列表 / 模糊搜索 / 单条详情 (纯标准库)
        │
        └── ssh_exec.py        取凭据 → paramiko SSH 登录 → 执行命令 / SFTP 传文件
                               (首次运行自动在 ~/.termora-ops/venv 自举安装 paramiko,
                                不污染系统 Python)
```

两个脚本都是**只读**的：不改动 Termora 数据；存储文件被 Termora 占用时自动复制到临时目录再解析。

## 安装（作为 Agent Skill）

把本目录复制到你的 Agent skill 目录即可（以 ZCode / Claude 系 Agent 为例）：

```bash
# 用户级安装
cp -r termora-ops ~/.agents/skills/
```

之后当你在对话中提到"登录服务器 / 查日志 / 重启服务 / 部署 / Termora 里的机器"而没给 IP 密码时，Agent 会自动触发该 skill。

也可以脱离 skill 单独当 CLI 工具用：

```bash
python scripts/termora_hosts.py                 # 列出全部主机 (凭据打码)
python scripts/ssh_exec.py --search 群晖 --exec "df -h"
```

## 使用示例

```bash
# 1. 搜索主机 (按名称/IP/用户/分组模糊匹配, 凭据已打码, 输出可安全展示)
python scripts/termora_hosts.py --search k3s

# 2. 执行远程命令
python scripts/ssh_exec.py --search 群晖 --exec "df -h"
python scripts/ssh_exec.py --name 腾讯云 --exec "cd /opt/app && git pull && systemctl restart app"

# 3. 需要 root 时 (自动探测免密 sudo, 否则用保存的密码走 stdin)
python scripts/ssh_exec.py --search k3s-master --exec "apt update && apt upgrade -y" --sudo

# 4. 传文件
python scripts/ssh_exec.py --search 群晖 --get /var/log/messages ./messages.log
python scripts/ssh_exec.py --search 群晖 --put ./deploy.sh --put-dst /tmp/deploy.sh

# 5. 长任务调整超时 (默认 60 秒)
python scripts/ssh_exec.py --search 群晖 --exec "tail -f /var/log/app.log" --timeout 300
```

## 退出码

| 退出码 | 含义 |
|---|---|
| 远程命令退出码 | 原样透传 |
| 0 | 成功 |
| 252 | 命令执行超时 |
| 253 | 主机匹配失败（无匹配 / 匹配到多台，错误信息里会列出候选） |
| 254 | 连接或认证失败（含未保存凭据、密钥缺失） |

## 安全设计

- **凭据零回显**：密码/私钥只在脚本进程内流转，不进命令行参数、不进输出。`termora_hosts.py` 所有输出模式默认打码（含嵌套 `authentication` 字段内的密码）。
- **`--show-secrets` 仅限调试**：会输出真实凭据，仅供脚本间联调，切勿把该输出贴进对话或日志。
- **sudo 密码走 stdin**：`--sudo` 通过 `sudo -S` 从 stdin 传密码，不出现在进程列表。
- **破坏性操作需人工确认**：skill 指令要求 Agent 对重启/删除/覆盖配置等操作先复述命令征得同意，只读探测（uptime、df、systemctl status）可直接执行。

## 文件结构

```
termora-ops/
├── SKILL.md                      # Agent skill 指令 (工作流 / 安全红线 / 参数速查)
├── README.md
├── references/
│   └── termora-storage.md        # Termora 存储格式逆向笔记 + 故障排查表
└── scripts/
    ├── termora_hosts.py          # 只读解析 Termora 存储 (纯标准库)
    └── ssh_exec.py               # SSH 执行命令 / SFTP 传文件 (自举 paramiko)
```

## 兼容性说明

- 存储格式基于 **Termora 1.0.17 (Windows)** 实测；Termora 升级更换存储引擎（如出现 `.mv.db`/`.db` 文件）时脚本会解析异常，排查思路见 `references/termora-storage.md`。
- 脚本兼容 Windows / Linux / macOS（venv 自举路径已做平台适配）；前提是 Termora 的存储位于 `~/.termora/storage/`。
- 主机未在 Termora 中保存凭据时无法自动登录（列表"认证"列显示 `-`），请先在 Termora 里保存账号密码或密钥。
