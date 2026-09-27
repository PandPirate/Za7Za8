#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PikPak 分享链接 -> 直链下载地址

把一个 PikPak 播放/分享链接（https://mypikpak.com/s/xxxx）解析成带签名的
可直接下载的 CDN 直链，无需登录 PikPak 账号。

用法::

    # 最简单的用法
    python pikpak_share_dl.py https://mypikpak.com/s/VP2DqofIa881OLWRaKYH0OYco2

    # 带提取码 + 走本地代理（大陆 IP 必须，见下方说明）
    python pikpak_share_dl.py <链接> -p 24rn --proxy http://127.0.0.1:7890

    # 输出 JSON / 直接下载
    python pikpak_share_dl.py <链接> --json
    python pikpak_share_dl.py <链接> --download ./downloads

注意:
  * PikPak 对**中国大陆 IP** 关闭了分享接口（返回 share_status=PROHIBITED，
    "分享功能在当前地区不可用"）。此时必须用 --proxy 指定一个境外代理。
    直链本身（dl-*.mypikpak.com）不做地区限制，拿到后大陆也能满速下载。
  * 直链是**带签名的临时地址**，有效期约 16 小时（响应里的 expire 字段），
    过期后重新跑一次本脚本即可。
  * 没有任何第三方依赖，只用标准库；socks 代理需要 PySocks。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# PikPak Web 端常量（从 mypikpak.com 的前端 SDK 中还原）
# ---------------------------------------------------------------------------

CLIENT_ID = "YUMx5nI8ZU8Ap8pm"
PACKAGE_NAME = "drive.mypikpak.com"
CLIENT_VERSION = "undefined"

USER_API = "https://user.mypikpak.com"
DRIVE_API = "https://api-drive.mypikpak.com/drive/v1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# 验证码签名用的盐值列表，顺序不能变
CAPTCHA_SALTS = [
    "fyZ4+p77W1U4zcWBUwefAIFhFxvADWtT1wzolCxhg9q7etmGUjXr",
    "uSUX02HYJ1IkyLdhINEFcCf7l2",
    "iWt97bqD/qvjIaPXB2Ja5rsBWtQtBZZmaHH2rMR41",
    "3binT1s/5a1pu3fGsN",
    "8YCCU+AIr7pg+yd7CkQEY16lDMwi8Rh4WNp5",
    "DYS3StqnAEKdGddRP8CJrxUSFh",
    "crquW+4",
    "ryKqvW9B9hly+JAymXCIfag5Z",
    "Hr08T/NDTX1oSJfHk90c",
    "i",
]

CHUNK = 256 * 1024


class PikPakError(Exception):
    """接口返回的业务错误。"""


class ShareError(PikPakError):
    """分享状态异常（不存在 / 过期 / 地区受限等）。"""


class NetworkError(PikPakError):
    """网络层错误（连不上目标、代理不可用等）。"""


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def gen_device_id() -> str:
    """32 位十六进制设备号，前端等同于 localStorage 里的 deviceid。"""
    return "".join(random.choice("0123456789abcdef") for _ in range(32))


def calc_captcha_sign(device_id: str, timestamp: str) -> str:
    """计算 captcha_sign：以 clientId+version+package+deviceId+timestamp 为初始值，
    依次和每个盐拼接做 md5，最后加 "1." 前缀。"""
    sign = CLIENT_ID + CLIENT_VERSION + PACKAGE_NAME + device_id + timestamp
    for salt in CAPTCHA_SALTS:
        sign = md5_hex(sign + salt)
    return "1." + sign


def human_size(num) -> str:
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if num < 1024 or unit == "TiB":
            return f"{num:.2f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024


def parse_link(link: str):
    """从分享链接里解析出 (share_id, file_id, pass_code)。

    支持这些写法::

        https://mypikpak.com/s/VP2DqofIa881OLWRaKYH0OYco2
        https://mypikpak.com/s/VP2DqofIa881OLWRaKYH0OYco2?pwd=1234
        https://mypikpak.com/s/VP2DqofIa881OLWRaKYH0OYco2/VP2DqnzRhvxgiz05VFNLur_2o2
        VP2DqofIa881OLWRaKYH0OYco2
    """
    link = link.strip()
    share_id = file_id = None
    pass_code = None

    if "://" in link or "/" in link:
        parsed = urllib.parse.urlparse(link if "://" in link else "https://" + link)
        parts = [p for p in parsed.path.split("/") if p]
        if "s" in parts:
            idx = parts.index("s")
            if len(parts) > idx + 1:
                share_id = parts[idx + 1]
            if len(parts) > idx + 2:
                file_id = parts[idx + 2]
        elif parts:
            share_id = parts[-1]
        query = urllib.parse.parse_qs(parsed.query)
        for key in ("pwd", "pass_code", "password", "code"):
            if query.get(key):
                pass_code = query[key][0]
                break
    else:
        share_id = link

    if not share_id or not re.fullmatch(r"[A-Za-z0-9_-]+", share_id):
        raise SystemExit(f"无法从 {link!r} 中解析出 share_id")
    return share_id, file_id, pass_code


class PikPakShare:
    """匿名访问 PikPak 分享的最小客户端。"""

    def __init__(self, proxy: str | None = None, timeout: int = 30, verbose: bool = False,
                 no_proxy: bool = False):
        self.device_id = gen_device_id()
        self.timestamp = str(int(time.time() * 1000))
        self.captcha_sign = calc_captcha_sign(self.device_id, self.timestamp)
        self.timeout = timeout
        self.verbose = verbose
        self._captcha_tokens: dict[str, str] = {}

        # urllib 默认会自动使用环境变量和 Windows 系统代理设置。这里显式区分三种情况，
        # 并记录“实际在用哪个出口”——代理没启动是这类脚本最常见的故障。
        if no_proxy:
            handlers = [urllib.request.ProxyHandler({})]   # 空字典 = 不使用任何代理
            self.proxy_desc = "直连（已忽略环境/系统代理）"
        elif proxy:
            handlers = self._proxy_handlers(proxy)
            self.proxy_desc = proxy
        else:
            handlers = []
            env_proxies = urllib.request.getproxies()
            picked = env_proxies.get("https") or env_proxies.get("http")
            self.proxy_desc = f"环境/系统代理 {picked}" if picked else "直连"
        self.opener = urllib.request.build_opener(*handlers)

        # 当前分享的上下文
        self.share_id: str | None = None
        self.pass_code: str = ""
        self.pass_code_token: str = ""
        self.global_file_token: str = ""

    # -- 底层 HTTP ---------------------------------------------------------

    @staticmethod
    def _proxy_handlers(proxy: str):
        if proxy.startswith(("socks4", "socks5", "socks")):
            try:
                import socks  # PySocks
            except ImportError:
                raise SystemExit("使用 socks 代理需要先安装 PySocks: pip install PySocks")
            parsed = urllib.parse.urlparse(proxy)
            proxy_type = (
                socks.SOCKS4 if proxy.startswith("socks4") else socks.SOCKS5
            )
            socks.set_default_proxy(
                proxy_type, parsed.hostname, parsed.port or 1080,
                rdns=(parsed.scheme or "").endswith("h") or True,
            )
            socket.socket = socks.socksocket
            return [urllib.request.ProxyHandler({})]
        return [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]

    def _http(self, method: str, url: str, headers: dict, body=None):
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers = dict(headers, **{"Content-Type": "application/json"})
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        if self.verbose:
            print(f"[http] {method} {url}", file=sys.stderr)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # 业务错误也是 4xx + JSON body，这里照常返回
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            # 连不上（含代理不可用）：翻译成能照着做的提示，而不是甩一个原始堆栈
            raise NetworkError(self._network_hint(url, exc.reason)) from None

    def _network_hint(self, url: str, reason) -> str:
        """把底层网络异常翻译成排查建议。"""
        host = urllib.parse.urlparse(url).hostname or url
        lines = [f"无法连接 {host}（出口: {self.proxy_desc}）：{reason}"]
        if "直连" not in self.proxy_desc:
            lines.append("这通常是代理不可用的症状（连接被拒绝 / 超时）。请确认："
                         "① 代理软件已启动；② 端口没写错；"
                         "③ 确实不需要代理时，把 --proxy 换成 --no-proxy")
        else:
            lines.append("请检查网络连通性；如果需要走代理，"
                         "用 --proxy http://127.0.0.1:7890 显式指定")
        return "\n".join(lines)

    @staticmethod
    def _loads(text: str):
        try:
            return json.loads(text)
        except ValueError:
            raise PikPakError(f"接口返回了非 JSON 内容: {text[:300]!r}")

    # -- 验证码 ------------------------------------------------------------

    def _captcha_token(self, action: str) -> str:
        """为某个 action 申请验证码令牌，例如 "GET:/drive/v1/share"。"""
        if action in self._captcha_tokens:
            return self._captcha_tokens[action]
        body = {
            "client_id": CLIENT_ID,
            "action": action,
            "device_id": self.device_id,
            "meta": {
                "captcha_sign": self.captcha_sign,
                "client_version": CLIENT_VERSION,
                "package_name": PACKAGE_NAME,
                "user_id": "",
                "timestamp": self.timestamp,
            },
        }
        status, text = self._http(
            "POST",
            f"{USER_API}/v1/shield/captcha/init",
            {
                "x-device-id": self.device_id,
                "x-client-id": CLIENT_ID,
                "User-Agent": USER_AGENT,
            },
            body,
        )
        data = self._loads(text)
        token = data.get("captcha_token")
        if not token:
            raise PikPakError(f"获取验证码令牌失败: {text[:300]}")
        self._captcha_tokens[action] = token
        return token

    # -- 业务接口 ----------------------------------------------------------

    def api(self, method: str, path: str, params: dict | None = None,
            body=None, headers: dict | None = None) -> dict:
        """调用 /drive/v1 下的接口，自动附带验证码令牌，遇到令牌失效自动重试一次。"""
        url = DRIVE_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        action = f"{method}:{path}"

        for attempt in range(2):
            head = {
                "x-device-id": self.device_id,
                "x-client-id": CLIENT_ID,
                "Accept-Language": "zh-CN",
                "Origin": "https://mypikpak.com",
                "Referer": "https://mypikpak.com/",
                "User-Agent": USER_AGENT,
            }
            head.update(headers or {})
            head["x-captcha-token"] = self._captcha_token(action)

            _, text = self._http(method, url, head, body)
            data = self._loads(text)

            if data.get("error") == "captcha_invalid" and attempt == 0:
                self._captcha_tokens.pop(action, None)  # 换一个新令牌重试
                continue
            if data.get("error"):
                raise PikPakError(
                    f"{data.get('error')}: {data.get('error_description', '')}"
                )
            return data
        raise PikPakError("验证码校验失败")

    def open_share(self, share_id: str, pass_code: str = "") -> dict:
        """读取分享信息，返回分享级元数据。"""
        self.share_id = share_id
        self.pass_code = pass_code or ""

        data = self.api("GET", "/share", {
            "share_id": share_id,
            "pass_code": self.pass_code,
            "limit": "100",
            "thumbnail_size": "SIZE_LARGE",
        })

        status = data.get("share_status")
        if status != "OK":
            text = data.get("share_status_text") or ""
            hint = ""
            if status == "PROHIBITED" or "地区" in text:
                hint = ("\n当前出口 IP 被地区限制，请用 --proxy 指定境外代理"
                        "（如 --proxy http://127.0.0.1:7890）")
            raise ShareError(f"分享不可用: share_status={status} {text}{hint}")

        self.pass_code_token = data.get("pass_code_token") or ""
        self.global_file_token = data.get("global_file_token") or ""
        return data

    def list_dir(self, parent_id: str | None = None) -> list:
        """列出分享根目录 / 子目录下的文件（自动翻页）。"""
        out, token = [], None
        while True:
            params = {
                "share_id": self.share_id,
                "limit": "100",
                "thumbnail_size": "SIZE_LARGE",
            }
            if parent_id:
                # 子目录走 /share/detail
                params["parent_id"] = parent_id
                params["pass_code_token"] = self.pass_code_token
                path = "/share/detail"
            else:
                params["pass_code"] = self.pass_code
                path = "/share"
            if token:
                params["next_page_token"] = token

            data = self.api("GET", path, params,
                            headers={"x-global-file-token": self.global_file_token})

            status = data.get("share_status")
            if status and status != "OK":
                raise ShareError(f"分享不可用: share_status={status}")

            entries = data.get("files") or []
            out.extend(entries)
            # 令牌可能在这一步才下发
            self.pass_code_token = data.get("pass_code_token") or self.pass_code_token
            self.global_file_token = data.get("global_file_token") or self.global_file_token

            token = data.get("next_page_token")
            if not token or not entries:
                break
        return out

    def walk(self, recursive: bool = True):
        """遍历分享里的所有条目，产出 (相对路径, 文件字典)。"""
        queue = [("", None)]
        while queue:
            prefix, parent_id = queue.pop(0)
            for entry in self.list_dir(parent_id):
                name = entry.get("name") or entry.get("id") or ""
                path = prefix + name
                if entry.get("kind") == "drive#folder":
                    if recursive:
                        queue.append((path + "/", entry.get("id")))
                    yield path + "/", entry
                else:
                    yield path, entry

    def file_info(self, file_id: str) -> dict:
        """获取单个文件的详情，其中 medias[].link.url 就是直链。"""
        data = self.api("GET", "/share/file_info", {
            "share_id": self.share_id,
            "file_id": file_id,
            "pass_code_token": self.pass_code_token,
            "thumbnail_size": "SIZE_LARGE",
        }, headers={"x-global-file-token": self.global_file_token})

        status = data.get("share_status")
        if status and status != "OK":
            raise ShareError(f"文件不可用: share_status={status}")
        return data.get("file_info") or data

    @staticmethod
    def extract_links(file_info: dict) -> list:
        """从 file_info 中提取所有可下载直链，按 原画优先、优先级降序 排列。"""
        links = []

        def add(url, name, resolution="", category="", expire="", is_origin=False,
                priority=0, size=None):
            if not url:
                return
            links.append({
                "name": name,
                "resolution": resolution,
                "category": category,
                "size": int(size) if str(size or "").isdigit() else size,
                "expire": expire,
                "is_origin": bool(is_origin),
                "priority": priority,
                "url": url,
            })

        for media in file_info.get("medias") or []:
            link = media.get("link") or {}
            add(
                link.get("url"),
                media.get("media_name") or media.get("resolution_name") or "media",
                resolution=media.get("resolution_name") or "",
                category=media.get("category") or "",
                expire=link.get("expire") or "",
                is_origin=media.get("is_origin") or False,
                priority=media.get("priority") or 0,
                size=file_info.get("size"),
            )

        # 部分文件（压缩包、文档等）直链在 links / web_content_link 里
        for key, value in (file_info.get("links") or {}).items():
            url = value.get("url") if isinstance(value, dict) else value
            add(url, key, category="direct", size=file_info.get("size"))
        add(file_info.get("web_content_link"), "web_content",
            category="direct", size=file_info.get("size"))

        links.sort(key=lambda x: (not x["is_origin"], -x["priority"]))
        return links


def download(url: str, dest: str, timeout: int = 30):
    """流式下载并打印进度。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = start = 0
        t0 = time.time()
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - start > 0.3:
                    start = now
                    speed = done / max(now - t0, 1e-6)
                    if total:
                        pct = done * 100 / total
                        print(f"\r  {pct:5.1f}%  {human_size(done)}/{human_size(total)}"
                              f"  {human_size(speed)}/s", end="", file=sys.stderr)
                    else:
                        print(f"\r  {human_size(done)}  {human_size(speed)}/s",
                              end="", file=sys.stderr)
    print(f"\r  完成: {dest} ({human_size(done)})" + " " * 24, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="PikPak 分享链接 -> 直链下载地址",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  %(prog)s https://mypikpak.com/s/xxxx\n"
               "  %(prog)s https://mypikpak.com/s/xxxx -p 提取码 --proxy http://127.0.0.1:7890\n"
               "  %(prog)s https://mypikpak.com/s/xxxx --json -o result.json\n"
               "  %(prog)s https://mypikpak.com/s/xxxx --download ./downloads\n"
               "  %(prog)s https://mypikpak.com/s/xxxx --no-proxy   # 忽略环境里的代理",
    )
    parser.add_argument("link", help="PikPak 分享链接或 share_id")
    parser.add_argument("-p", "--pass-code", default=None, help="分享提取码")
    parser.add_argument("--proxy", default=None,
                        help="HTTP 代理，例如 http://127.0.0.1:7890（socks5 需 PySocks）")
    parser.add_argument("--no-proxy", action="store_true",
                        help="忽略环境变量 / 系统里的代理，强制直连")
    parser.add_argument("--no-recursive", action="store_true", help="不进入文件夹")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("-o", "--output", help="把结果写入文件")
    parser.add_argument("--download", metavar="DIR", help="直接下载到指定目录")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印请求细节")
    args = parser.parse_args(argv)

    if args.proxy and args.no_proxy:
        parser.error("--proxy 与 --no-proxy 不能同时使用")

    share_id, file_id, link_pass_code = parse_link(args.link)
    pass_code = args.pass_code if args.pass_code is not None else (link_pass_code or "")

    client = PikPakShare(proxy=args.proxy, verbose=args.verbose, no_proxy=args.no_proxy)

    if args.verbose:
        print(f"[info] device_id={client.device_id}", file=sys.stderr)
        print(f"[info] 出口: {client.proxy_desc}", file=sys.stderr)

    try:
        meta = client.open_share(share_id, pass_code)

        targets = []
        if file_id:
            targets.append((meta.get("title") or file_id, {"id": file_id}))
        else:
            for path, entry in client.walk(recursive=not args.no_recursive):
                if entry.get("kind") == "drive#folder":
                    continue
                targets.append((path, entry))

        results = []
        for path, entry in targets:
            fid = entry.get("id")
            if not fid:
                continue
            info = client.file_info(fid)
            results.append({
                "path": path,
                "id": fid,
                "name": info.get("name") or entry.get("name") or "",
                "size": int(info.get("size") or entry.get("size") or 0) or None,
                "mime_type": info.get("mime_type") or "",
                "duration": (info.get("params") or {}).get("duration"),
                "hash": info.get("hash") or "",
                "download_links": PikPakShare.extract_links(info),
            })
    except PikPakError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    result = {
        "share_id": share_id,
        "title": meta.get("title") or "",
        "author": (meta.get("user_info") or {}).get("nickname") or "",
        "file_num": meta.get("file_num"),
        "files": results,
    }

    if args.json or args.output:
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            print(f"已写入 {args.output}")
        if args.json:
            print(text)
    else:
        print(f"分享: {result['title'] or share_id}    作者: {result['author']}")
        for item in results:
            size = human_size(item["size"]) if item["size"] else "-"
            dur = item["duration"]
            extra = f"  时长: {int(dur) // 60}分{int(dur) % 60}秒" if str(dur or "").isdigit() else ""
            print(f"\n{item['path']}")
            print(f"  {size}  {item['mime_type']}{extra}")
            if not item["download_links"]:
                print("  (没有可用直链)")
            for link in item["download_links"]:
                tag = "原画" if link["is_origin"] else (link["resolution"] or link["category"] or "转码")
                print(f"  [{tag}] 过期: {link['expire'] or '-'}")
                print(f"  {link['url']}")

    if args.download:
        os.makedirs(args.download, exist_ok=True)
        for item in results:
            if not item["download_links"]:
                continue
            best = item["download_links"][0]
            dest = os.path.join(args.download, os.path.basename(item["name"] or item["id"]))
            print(f"下载 {dest}", file=sys.stderr)
            download(best["url"], dest)

    return 0


if __name__ == "__main__":
    sys.exit(main())
