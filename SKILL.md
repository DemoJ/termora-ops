---
name: termora-ops
description: 读取本机 Termora 终端软件保存的服务器配置（主机名/IP/端口/账号/密码/SSH密钥），让 agent 不需要用户提供 IP 和密码即可自动 SSH 登录服务器执行运维命令、传文件。只要用户提到登录/连接/操作服务器、执行远程命令、查日志、重启服务、部署，却没给出 IP 和账号密码；或直接说了主机名/别名（如 群晖、k3s-master、腾讯云、PVE）；或提到 Termora、termora 里的机器——都应使用本 skill。触发词：服务器、运维、SSH、远程执行、Termora、登录主机。
---

# Termora 凭据自动化运维

本机 Termora（`~/.termora/storage/`）保存了用户的服务器连接信息。本 skill 通过两个只读解析脚本拿到主机列表和凭据，再自动 SSH 执行命令——用户只需说"上群晖看看磁盘"，不需要提供任何 IP 密码。

- `scripts/termora_hosts.py` — 列出/搜索 Termora 中的主机（纯标准库，凭据默认打码）
- `scripts/ssh_exec.py` — 用 Termora 里的凭据 SSH 登录并执行命令/传文件（首次运行自动在 `~/.termora-ops/venv` 自举安装 paramiko，约 10–30 秒，不动系统 Python）

以下脚本路径均为 skill 目录下的相对路径，实际调用时替换为绝对路径。

## 安全红线（先读）

1. **绝不把密码/私钥写进对话、日志或任何文件。** 凭据只在脚本进程内流转；`termora_hosts.py` 默认打码，保持默认。
2. **不要用 `--show-secrets` 把凭据输出到对话里。** 该参数只用于脚本间调试，一旦用了，凭据就进了聊天记录。
3. **破坏性操作必须先确认。** 重启/关机、删除文件、覆盖配置、改防火墙/端口、drop 数据库、生产环境写操作——先向用户复述将要执行的命令并得到同意，再执行。只读探测（uptime、df、systemctl status、cat 日志）可以直接做。
4. **不确定哪台机器时，列出来让用户选**，不要按相似名字猜着连（如 "notebook" 和 "笔记本" 是两台不同的机器）。

## 标准工作流

### 1. 用户提到某台服务器 → 搜索主机

```bash
python <skill>/scripts/termora_hosts.py --search 关键词
```

关键词可以是名称、IP、用户名、分组的子串（如 `--search k3s`、`--search 192.168`）。直接运行不带参数则列出全部主机。凭据列已打码，此输出可安全展示。

### 2. 执行远程命令

```bash
python <skill>/scripts/ssh_exec.py --search 群晖 --exec "df -h"
```

- 匹配规则同上；匹配到多台会列出候选并退出（码 253），此时向用户展示候选让其选择，或换 `--name 精确名`。
- 多步操作组合成一条命令：`--exec "cd /opt/app && git pull && systemctl restart app"`；或分多次调用，每步看结果再下一步。
- 需要 root：`--sudo`（脚本自动探测免密 sudo，否则用 Termora 保存的密码走 stdin，不进命令行）。
- 超时默认 60 秒，长任务用 `--timeout 300`。

### 3. 传文件

```bash
python <skill>/scripts/ssh_exec.py --search 群晖 --get /var/log/messages ./messages.log
python <skill>/scripts/ssh_exec.py --search 群晖 --put ./deploy.sh --put-dst /tmp/deploy.sh
```

### 4. 解读输出

- 首行 `[连接] 名称 (user@host:port)` 确认连到了哪台机器——把它和用户要求核对。
- 远程命令的退出码原样透传；`252`=超时、`253`=主机匹配失败、`254`=连接/认证失败。
- stderr 里远程 sshd 的 `Could not chdir to home directory` 警告（群晖常见）无害，可忽略。

## 边界情况

- **存储位置被搬迁**：脚本按 Termora 官方顺序自动发现存储文件（`--file` 显式指定 > 环境变量 `TERMORA_BASE_DATA_DIR` > 默认 `~/.termora`），一般无需干预；全部落空时按报错信息排查（确认 Termora 能否打开主机列表、检查 Termora 启动参数 `-Dtermora.base-data-dir`）。
- **Termora 正在运行**：存储文件可能被锁，脚本会自动复制到临时目录再解析，无需干预。
- **主机没有保存凭据**（列表"认证"列显示 `-`）：告诉用户先在 Termora 里给这台机器保存账号密码/密钥，脚本无法登录。
- **命令输出为空但退出码非 0**：先加 `2>&1` 重跑看 stderr，再判断是权限还是命令本身的问题。
- **搜索不到主机 / 解析异常**：新版 Termora 可能更换了存储格式，读 `references/termora-storage.md` 里的排查步骤。

## 参数速查

| 脚本 | 参数 | 说明 |
|---|---|---|
| termora_hosts.py | （无） | 列出全部主机 |
| | `--search kw` | 模糊搜索 |
| | `--json [--keys]` | 机器可读 JSON（默认打码） |
| | `--show 名称\|ID` | 查看单条详情 |
| | `--show-secrets` | 不打码（仅调试，禁止输出到对话） |
| ssh_exec.py | `--search kw` / `--name 名称` / `--id id` | 选主机（三选一） |
| | `--exec "命令"` | 执行远程命令 |
| | `--sudo` | sudo 执行 |
| | `--get 远程 --get-dst 本地` | 下载文件 |
| | `--put 本地 --put-dst 远程` | 上传文件 |
| | `--timeout 秒` | 命令超时（默认 60） |
