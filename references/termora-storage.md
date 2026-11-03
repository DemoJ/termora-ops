# Termora 本地存储格式说明

记录本 skill 依赖的 Termora 数据格式与排查方法，基于 **Termora 1.0.17（Windows，2025-06 安装包）** 实测。

## 数据位置

```
~/.termora/
├── storage/
│   ├── 00000000000.xd    ← 核心数据: 主机/分组/密钥/设置 (MapDB 库, 本 skill 解析这个文件)
│   └── xd.lck            ← Termora 运行时的锁文件
├── known_hosts           ← Termora 自己记录的 SSH host key
└── logs/                 ← 应用日志
```

安装目录（`C:\Program Files\Termora\`）只有程序本体，`app\Termora.cfg` 可看版本号。

### 存储位置解析顺序（源码 `Application.getBaseDataDir`，1.0.17 与最新版一致）

1. 启动参数 `-Dtermora.base-data-dir=<路径>`（Termora 快捷方式/启动脚本里设置，外部脚本探测不到）
2. 环境变量 `TERMORA_BASE_DATA_DIR`
3. 默认 `~/.termora`

存储被搬迁时 `.xd` 文件位于 `<数据目录>/storage/` 下，文件名仍为 `00000000000.xd`。本 skill 的两个脚本按同样顺序自动发现（`storage/` 下主文件名变化时取最新 `.xd` 兜底）；也可用 `--file` 显式指定。

## .xd 文件内部结构

MapDB（JVM 嵌入式 KV 库）顺序存储 + 旧版本记录残留，无加密。三类业务记录都是完整 JSON 字符串，以 `{"id":"<32位hex>"` 开头：

### 主机记录（protocol ≠ Folder 且含 host 字段）

```json
{"id":"...","name":"群晖","protocol":"SSH","host":"10.0.0.2","port":22,"username":"youruser",
 "authentication":{"type":"Password","password":"明文密码"},
 "sort":9,"createDate":1742648615839,"updateDate":1742649003028}
```

- `authentication.type`：`Password`（`password`=登录密码明文）或 `PublicKey`（`password`=密钥记录的 **id**，不是密码）。
- 认证列显示 `-` = 未保存凭据，无法自动登录。
- 可能还有 `options`（如 x11Forwarding）、`comment`、`deleted:true`（已删除）。

### 分组记录

```json
{"id":"...","name":"Sessions","protocol":"Folder","parentId":"<父分组id>"}
```

- `parentId` 为空/`"0"`/`"root"` = 顶层。主机记录多数情况下 parentId 为空（平铺）。

### 密钥记录（含 privateKey 字段）

```json
{"id":"...","name":"tengxunyun_key","type":"RSA","length":4096,
 "publicKey":"ssh-rsa AAAA...","privateKey":"MIIJQgIBADANBgkqhkiG9w0BAQEF...","updateDate":...}
```

- `privateKey` 是 **PKCS#8 DER 的 base64**（`MII` 开头），无 PEM 头。无口令字段；若某天出现 `passphrase`/`password` 字段即为密钥口令。
- `ssh_exec.py` 用 cryptography 加载 DER → 转 PEM → paramiko 使用。

## 去重规则（重要）

MapDB 保留历史版本：同一个 id 会出现多份记录（含 `deleted:true` 的旧版）。解析时必须：

1. 按 `id` 分组，取 `updateDate`（无则 `createDate`）最大的那份；
2. 最新版若 `deleted:true` 则丢弃。

`termora_hosts.py` 的 `iter_records` / `latest_by_id` 已实现：扫描 `{"id":` 标记 → 平衡括号截取完整 JSON → `json.loads` 校验。

## 排查步骤

| 现象 | 原因/处理 |
|---|---|
| 文件被占用 (PermissionError) | Termora 正在运行；脚本已自动复制到临时目录解析，无需干预 |
| 列表为空 | 用户从未保存过主机，或存储路径不同；用 `--file` 指定实际路径 |
| 搜索不到刚添加的主机 | Termora 可能尚未把内存数据刷盘；让用户在 Termora 里 Ctrl+S 保存或退出后重试 |
| `--json` 解析出大量 Setting 记录 | 正常，`hosts_of`/`find_keys` 只保留主机与密钥 |
| 存储目录出现 `.mv.db` / `.db` 等其他文件 | 新版 Termora 换了存储引擎（如 SQLite/H2）；不要硬扫，改用对应库读取，表结构先让 agent 探查 |
| 密钥登录报 `无法解析私钥` | 密钥带口令而 Termora 未存口令字段；提示用户在 Termora 里重新导入无口令密钥或改用密码认证 |
