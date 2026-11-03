#!/usr/bin/env python3
"""读取本机 Termora 终端存储的主机/分组/密钥信息。

数据来源: ~/.termora/storage/00000000000.xd (MapDB 存储, 每条记录是一个 JSON 字符串)。
本脚本只读: 优先直接读取, 被占用时复制到临时目录再解析, 不改动 Termora 数据。

用法:
  python termora_hosts.py                     # 列出全部主机 (凭据打码)
  python termora_hosts.py --search k3s        # 按名称/IP/用户/分组模糊搜索
  python termora_hosts.py --json              # 输出机器可读 JSON (默认打码)
  python termora_hosts.py --json --show-secrets   # 输出含真实凭据的 JSON (慎用, 勿回显到对话)
  python termora_hosts.py --show k3s          # 查看单台主机详情
  python termora_hosts.py --keys              # 查看已保存的 SSH 密钥
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import unicodedata

DEFAULT_STORAGE = os.path.join(os.path.expanduser("~"), ".termora", "storage", "00000000000.xd")
# 任何输出模式下默认都要打码的字段
SECRET_FIELDS = ("password", "passphrase", "privateKey")


def read_storage(path: str) -> bytes:
    """读取存储文件; 被占用 (Termora 正在运行) 时复制到临时目录再读。"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except (PermissionError, OSError):
        fd, tmp = tempfile.mkstemp(suffix=".xd")
        try:
            shutil.copyfile(path, tmp)
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            os.close(fd)
            os.unlink(tmp)


def _balanced_json(data: bytes, start: int) -> bytes | None:
    """从 start (应为 '{') 开始截取一个完整的平衡 JSON 对象。"""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(data)):
        b = data[i]
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:  # backslash
                esc = True
            elif b == 0x22:  # quote
                in_str = False
        elif b == 0x22:
            in_str = True
        elif b == 0x7B:  # {
            depth += 1
        elif b == 0x7D:  # }
            depth -= 1
            if depth == 0:
                return data[start : i + 1]
    return None


def iter_records(data: bytes) -> list[dict]:
    """扫描文件中所有 JSON 记录 (含历史版本), 解析失败或无 id 的跳过。"""
    out: list[dict] = []
    pos = 0
    marker = b'{"id":'
    while True:
        pos = data.find(marker, pos)
        if pos < 0:
            break
        raw = _balanced_json(data, pos)
        if raw is None:
            pos += 1
            continue
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("id"), str):
            out.append(obj)
            pos += len(raw)
        else:
            pos += 1
    return out


def latest_by_id(records: list[dict], include_deleted: bool = False) -> dict[str, dict]:
    """同一 id 存在多个历史版本时, 取 updateDate/createDate 最新的那条。"""
    best: dict[str, dict] = {}
    for rec in records:
        rid = rec["id"]
        stamp = max(rec.get("updateDate") or 0, rec.get("createDate") or 0)
        cur = best.get(rid)
        if cur is None or stamp >= max(cur.get("updateDate") or 0, cur.get("createDate") or 0):
            best[rid] = rec
    if include_deleted:
        return best
    return {rid: r for rid, r in best.items() if not r.get("deleted")}


def build_paths(by_id: dict[str, dict]) -> dict[str, str]:
    """根据 parentId 拼出每条记录的分组路径, 如 'Xshell/Sessions'。"""
    paths: dict[str, str] = {}

    def path_of(rid: str, depth: int = 0) -> str:
        if rid in paths:
            return paths[rid]
        rec = by_id.get(rid)
        if rec is None or depth > 20:  # 防御: 父节点缺失或环
            return ""
        parent = rec.get("parentId")
        prefix = path_of(parent, depth + 1) if isinstance(parent, str) and parent not in ("", "0", "root") else ""
        name = rec.get("name") or ""
        paths[rid] = f"{prefix}/{name}" if prefix else name
        return paths[rid]

    for rid in by_id:
        path_of(rid)
    return paths


def load_storage(path: str | None = None, include_deleted: bool = False):
    """返回 (records, paths): 去重后的全部记录 + 各记录的分组路径。"""
    path = path or DEFAULT_STORAGE
    if not os.path.exists(path):
        sys.exit(f"[错误] 找不到 Termora 存储文件: {path}")
    records = iter_records(read_storage(path))
    by_id = latest_by_id(records, include_deleted)
    return by_id, build_paths(by_id)


def hosts_of(by_id: dict[str, dict]) -> list[dict]:
    """有 host 字段的记录即主机 (SSH/Telnet/Serial 等)。"""
    return [r for r in by_id.values() if isinstance(r.get("host"), str)]


def find_keys(by_id: dict[str, dict]) -> list[dict]:
    """含 privateKey 的记录为密钥库中的 SSH 密钥。"""
    return [r for r in by_id.values() if isinstance(r.get("privateKey"), str)]


def mask(obj: dict, show_secrets: bool = False) -> dict:
    """复制记录并打码敏感字段 (含 authentication 等嵌套字典内的字段)。"""
    out = dict(obj)
    if not show_secrets:
        for key, val in out.items():
            if isinstance(val, dict):
                out[key] = mask(val, show_secrets)
            elif isinstance(val, str) and key in SECRET_FIELDS:
                out[key] = "***"
    for field in ("privateKey", "publicKey"):
        if isinstance(out.get(field), str) and len(out[field]) > 60 and not show_secrets:
            out[field] = "***"
        elif isinstance(out.get(field), str) and len(out[field]) > 120:
            out[field] = out[field][:120] + "..."
    return out


def host_endpoint(rec: dict) -> str:
    return f"{rec.get('username', '?')}@{rec.get('host')}:{rec.get('port', 22)}"


def auth_summary(rec: dict) -> str:
    auth = rec.get("authentication") or {}
    t = auth.get("type", "-")
    return str(t)


def display_width(s: str) -> int:
    """东亚全角字符按 2 列宽计算, 保证表格对齐。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - display_width(s))


def render_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], display_width(cell))
    line = "  ".join(pad(h, widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(pad(cell, widths[i]) for i, cell in enumerate(row)))


def match(rec: dict, path: str, keyword: str) -> bool:
    kw = keyword.lower()
    hay = " ".join(
        str(rec.get(k, "")) for k in ("name", "host", "username", "comment")
    ) + " " + path
    return kw in hay.lower()


def parent_path(paths: dict[str, str], rid: str) -> str:
    """记录所属分组的路径 (不含记录自身的名字)。"""
    path = paths.get(rid, "")
    return path.rsplit("/", 1)[0] if "/" in path else ""


def cmd_list(args) -> None:
    by_id, paths = load_storage(args.file, args.include_deleted)
    hosts = sorted(hosts_of(by_id), key=lambda r: (parent_path(paths, r["id"]), r.get("name", "")))
    rows = []
    for rec in hosts:
        path = parent_path(paths, rec["id"])
        if args.search and not match(rec, path, args.search):
            continue
        rows.append(
            [
                rec.get("name", "(未命名)"),
                path,
                rec.get("protocol", "SSH"),
                host_endpoint(rec),
                auth_summary(rec),
                rec.get("comment", "") or "",
            ]
        )
    if not rows:
        print(f"[提示] 没有匹配的主机{f' (关键词: {args.search})' if args.search else ''}")
        return
    print(f"共 {len(rows)} 台主机:\n")
    render_table(["名称", "分组", "协议", "登录信息", "认证", "备注"], rows)


def cmd_json(args) -> None:
    by_id, paths = load_storage(args.file, args.include_deleted)
    hosts, keys = [], []
    for rec in sorted(hosts_of(by_id), key=lambda r: (parent_path(paths, r["id"]), r.get("name", ""))):
        path = parent_path(paths, rec["id"])
        if args.search and not match(rec, path, args.search):
            continue
        item = mask(rec, args.show_secrets)
        item["_group"] = path
        hosts.append(item)
    if args.keys:
        for key in find_keys(by_id):
            keys.append(mask(key, args.show_secrets))
    print(json.dumps({"hosts": hosts, **({"keys": keys} if args.keys else {})},
                     ensure_ascii=False, indent=2))


def cmd_show(args) -> None:
    by_id, paths = load_storage(args.file, args.include_deleted)
    found = [r for r in by_id.values() if args.target in (r.get("name"), r["id"])
             or args.target.lower() in (r.get("name") or "").lower()]
    if len(found) != 1:
        names = ", ".join(f"{r.get('name')}({r['id'][:8]})" for r in found[:10])
        sys.exit(f"[错误] --show 需要唯一匹配, 当前匹配 {len(found)} 条: {names or '无'}")
    print(json.dumps(mask(found[0], args.show_secrets), ensure_ascii=False, indent=2))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="读取 Termora 存储的主机/分组/密钥信息 (只读)")
    ap.add_argument("--search", help="按名称/IP/用户/分组模糊搜索")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--keys", action="store_true", help="附带密钥列表 (配合 --json)")
    ap.add_argument("--show", metavar="NAME|ID", help="查看单条记录详情")
    ap.add_argument("--show-secrets", action="store_true", help="不打码敏感字段 (慎用, 勿回显到对话)")
    ap.add_argument("--include-deleted", action="store_true", help="包含已删除记录")
    ap.add_argument("--file", default=DEFAULT_STORAGE, help="存储文件路径 (默认 ~/.termora/storage/00000000000.xd)")
    args = ap.parse_args()

    if args.show:
        cmd_show(args)
    elif args.json:
        cmd_json(args)
    else:
        cmd_list(args)


if __name__ == "__main__":
    main()
