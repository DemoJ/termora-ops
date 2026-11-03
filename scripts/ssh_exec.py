#!/usr/bin/env python3
"""用 Termora 中保存的凭据自动 SSH 登录服务器并执行命令。

凭据来自本机 Termora 存储 (通过 termora_hosts.py 解析), 本脚本不输出任何密钥口令。

用法:
  python ssh_exec.py --search k3s --exec "uptime"        # 模糊匹配主机并执行命令
  python ssh_exec.py --name 腾讯云 --exec "df -h"         # 按名称精确匹配
  python ssh_exec.py --search xx --exec "apt update" --sudo
  python ssh_exec.py --search xx --get /var/log/app.log ./app.log
  python ssh_exec.py --search xx --put ./app.log /tmp/app.log

退出码: 远程命令原样透传; 252=超时 253=主机匹配失败 254=连接/认证失败
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from termora_hosts import (  # noqa: E402
    hosts_of,
    load_storage,
    parent_path,
)

CONNECT_TIMEOUT = 10
VENV_DIR = os.path.join(os.path.expanduser("~"), ".termora-ops", "venv")


def _venv_python() -> str:
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def ensure_paramiko() -> None:
    """paramiko 缺失时, 在独立 venv (~/.termora-ops/venv) 中自举安装并重启自身, 不污染系统 Python。"""
    try:
        import paramiko  # noqa: F401
        return
    except ImportError:
        pass

    # 统一落到专用 venv, 保证依赖环境稳定、不污染系统 Python
    py = _venv_python()
    if not os.path.exists(py):
        uv = shutil.which("uv")
        if uv:
            subprocess.run([uv, "venv", VENV_DIR], check=True, capture_output=True)
        else:
            subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True)
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--python", py, "paramiko"]
    else:
        cmd = [py, "-m", "pip", "install", "paramiko"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("[错误] 自动安装 paramiko 失败:\n" + (r.stderr or r.stdout)[-800:]
                 + f"\n可手动执行: {py} -m pip install paramiko")
    # 用 subprocess 而非 os.execv: Windows 的 execv 不给含空格的参数加引号
    sys.exit(subprocess.call([py, os.path.abspath(__file__)] + sys.argv[1:]))


def select_host(args):
    """按 --id / --name / --search 选取唯一主机; 匹配多条/零条时列出候选。"""
    by_id, paths = load_storage(getattr(args, "file", None))
    hosts = hosts_of(by_id)
    if args.id:
        pool = [h for h in hosts if h["id"] == args.id]
        kw = args.id
    elif args.name:
        pool = [h for h in hosts if h.get("name") == args.name]
        kw = args.name
    else:
        low = args.search.lower()
        pool = [
            h for h in hosts
            if low in (h.get("name") or "").lower()
            or low in (h.get("host") or "").lower()
            or low in (h.get("username") or "").lower()
            or low in parent_path(paths, h["id"]).lower()
        ]
        kw = args.search
    if len(pool) == 1:
        return pool[0]
    lines = [f"  - {h.get('name')}  ({h.get('username')}@{h.get('host')}:{h.get('port', 22)})"
             for h in sorted(pool, key=lambda r: r.get("name") or "")]
    if not pool:
        print(f"[错误] 没有匹配 '{kw}' 的主机。Termora 中的主机:\n" +
              "\n".join(f"  - {h.get('name')}  ({h.get('username')}@{h.get('host')}:{h.get('port', 22)})"
                        for h in sorted(hosts, key=lambda r: r.get("name") or "")),
              file=sys.stderr)
        sys.exit(253)
    print(f"[错误] '{kw}' 匹配到 {len(pool)} 台主机, 请用 --name/--id 或更精确的关键词:\n" + "\n".join(lines),
          file=sys.stderr)
    sys.exit(253)


def load_private_key(key_rec: dict):
    """把 Termora 密钥记录 (PKCS#8 DER base64) 转成 paramiko 密钥对象。"""
    import paramiko
    from cryptography.hazmat.primitives import serialization

    der = base64.b64decode(key_rec["privateKey"])
    passphrase = key_rec.get("passphrase") or key_rec.get("password") or None
    pkey = serialization.load_der_private_key(der, password=passphrase.encode() if passphrase else None)
    pem = pkey.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    pem = pem if isinstance(pem, str) else pem.decode()
    errors = []
    for cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(pem))
        except (paramiko.ssh_exception.SSHException, ValueError) as e:
            errors.append(str(e))
    raise RuntimeError("无法解析私钥: " + "; ".join(errors[:3]))


def auth_params(host_rec: dict, key_records: dict) -> dict:
    """根据主机记录的认证方式生成 paramiko connect 参数 (不含任何明文回显)。"""
    import paramiko

    auth = host_rec.get("authentication") or {}
    t = auth.get("type")
    if t == "Password" and auth.get("password"):
        return {"password": auth["password"]}
    if t == "PublicKey":
        key = key_records.get(auth.get("password"))
        if key is None:
            print("[错误] 主机使用密钥登录, 但在 Termora 密钥库中找不到对应密钥记录", file=sys.stderr)
            sys.exit(254)
        return {"pkey": load_private_key(key)}
    print("[错误] 该主机在 Termora 中未保存可用凭据 (认证类型: "
          f"{t or '未设置'}), 请先在 Termora 里保存账号密码/密钥", file=sys.stderr)
    sys.exit(254)


def connect(host_rec: dict, key_records: dict):
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host_rec["host"],
        port=int(host_rec.get("port") or 22),
        username=host_rec.get("username"),
        timeout=CONNECT_TIMEOUT,
        banner_timeout=CONNECT_TIMEOUT,
        auth_timeout=CONNECT_TIMEOUT,
        allow_agent=False,
        look_for_keys=False,
        **auth_params(host_rec, key_records),
    )
    return client


def run_command(client, cmd: str, password: str | None, sudo: bool, timeout: float) -> int:
    """执行命令; --sudo 时先探测免密 sudo, 否则通过 stdin 传入 sudo 密码 (不出现在命令行)。"""
    stdin, stdout, stderr = None, None, None
    if sudo:
        probe = client.exec_command("sudo -n true 2>/dev/null")
        if probe[1].channel.recv_exit_status() == 0:
            stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        else:
            if not password:
                print("[错误] 需要 sudo 密码但主机记录里没有保存密码", file=sys.stderr)
                sys.exit(254)
            stdin, stdout, stderr = client.exec_command("sudo -S -p '' -k bash -s", timeout=timeout)
            stdin.write(password + "\n" + cmd)
            stdin.channel.shutdown_write()
    else:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)

    channel = stdout.channel
    deadline = time.time() + timeout
    out, err = b"", b""
    while True:
        if channel.recv_ready():
            out += channel.recv(65536)
        if channel.recv_stderr_ready():
            err += channel.recv_stderr(65536)
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            break
        if time.time() > deadline:
            channel.close()
            print(out.decode("utf-8", "replace"), end="")
            print(err.decode("utf-8", "replace"), end="", file=sys.stderr)
            print(f"[错误] 命令执行超过 {timeout:.0f} 秒被终止", file=sys.stderr)
            sys.exit(252)
        time.sleep(0.05)
    while channel.recv_ready():
        out += channel.recv(65536)
    while channel.recv_stderr_ready():
        err += channel.recv_stderr(65536)
    code = channel.recv_exit_status()
    sys.stdout.write(out.decode("utf-8", "replace"))
    err_text = err.decode("utf-8", "replace")
    if err_text:
        sys.stderr.write(err_text)
    return code


def do_sftp(client, args) -> None:
    sftp = client.open_sftp()
    try:
        if args.get:
            sftp.get(args.get, args.get_dst)
            print(f"[完成] 已下载 {args.get} -> {args.get_dst}")
        if args.put:
            sftp.put(args.put, args.put_dst)
            print(f"[完成] 已上传 {args.put} -> {args.put_dst}")
    finally:
        sftp.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="用 Termora 凭据执行远程命令")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--search", help="按名称/IP/用户/分组模糊匹配主机")
    g.add_argument("--name", help="按名称精确匹配主机")
    g.add_argument("--id", help="按记录 id 精确匹配主机")
    ap.add_argument("--exec", dest="exec_cmd", help="要执行的远程命令")
    ap.add_argument("--sudo", action="store_true", help="用保存的密码执行 sudo 命令")
    ap.add_argument("--get", metavar="REMOTE", help="下载远程文件 (配合 --get-dst)")
    ap.add_argument("--get-dst", default=".")
    ap.add_argument("--put", metavar="LOCAL", help="上传本地文件 (配合 --put-dst)")
    ap.add_argument("--put-dst", required=False)
    ap.add_argument("--timeout", type=float, default=60, help="命令超时秒数 (默认 60)")
    ap.add_argument("--file", default=None,
                    help="Termora 存储文件路径 (默认自动发现: 环境变量 TERMORA_BASE_DATA_DIR > ~/.termora)")
    args = ap.parse_args()

    try:
        ensure_paramiko()
    except Exception as e:
        sys.exit(f"[错误] 依赖准备失败: {e}")

    if not args.put_dst and args.put:
        ap.error("--put 需要同时提供 --put-dst 远程路径")

    host = select_host(args)
    by_id, _ = load_storage(args.file)
    key_records = {r["id"]: r for r in by_id.values() if isinstance(r.get("privateKey"), str)}
    print(f"[连接] {host.get('name')} ({host.get('username')}@{host.get('host')}:{host.get('port', 22)})")

    try:
        client = connect(host, key_records)
    except Exception as e:
        print(f"[错误] 连接/认证失败: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(254)

    try:
        if args.get or args.put:
            do_sftp(client, args)
        elif args.exec_cmd:
            password = (host.get("authentication") or {}).get("password") \
                if (host.get("authentication") or {}).get("type") == "Password" else None
            code = run_command(client, args.exec_cmd, password, args.sudo, args.timeout)
            sys.exit(code)
        else:
            sys.exit("[错误] 请指定 --exec / --get / --put 之一")
    finally:
        client.close()


if __name__ == "__main__":
    main()
