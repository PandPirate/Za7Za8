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
import http.client
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
                priority=0, size=None, container=""):
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
                "container": container,
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
                container=(media.get("video") or {}).get("video_type") or "",
            )

        # 部分文件（压缩包、文档等）直链在 links / web_content_link 里
        for key, value in (file_info.get("links") or {}).items():
            url = value.get("url") if isinstance(value, dict) else value
            add(url, key, category="direct", size=file_info.get("size"))
        add(file_info.get("web_content_link"), "web_content",
            category="direct", size=file_info.get("size"))

        links.sort(key=lambda x: (not x["is_origin"], -x["priority"]))
        return links


class TruncatedError(PikPakError):
    """下载收到的字节数少于服务器声明的长度。"""


# 下载过程中值得重试的异常。刻意不用宽泛的 OSError：磁盘写满之类的本地
# 错误应该直接暴露出来，而不是被当成网络抖动重试几十次。
NETWORK_ERRORS = (
    urllib.error.URLError,      # 连接失败、超时、HTTP 4xx/5xx（HTTPError 是它的子类）
    http.client.HTTPException,  # 响应没读完就断开（IncompleteRead 等）
    ConnectionError,            # 连接被重置/中断（ConnectionResetError 等）
    socket.timeout,             # 读超时（Python 3.10 起等同于 TimeoutError）
)


def build_opener(proxy: str | None = None, no_proxy: bool = False):
    """按「显式代理 / 显式不走代理 / 交给系统」三种情况构造 opener。

    第三种有坑：不传 ``ProxyHandler`` 时，urllib 会装上**默认的** ProxyHandler，
    它会读 ``http_proxy`` / ``https_proxy`` 环境变量和 Windows 系统代理设置。
    所以「传 None 就等于直连」是错的——真要直连必须给一个空的
    ``ProxyHandler({})``。
    """
    if no_proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def part_path(dest: str) -> str:
    """下载中的临时文件名。

    只有在「字节数校验通过」之后才改名成 dest，这样磁盘上永远不会
    出现一个「看起来正常、其实只有一半」的成品文件。
    """
    return dest + ".part"


def remote_size(url: str, proxy: str | None = None, no_proxy: bool = False,
                timeout: int = 30):
    """用 Range: bytes=0-0 换回 Content-Range，读出服务器端的真实总长度。

    只取 1 个字节，不下载整个文件。失败返回 None。
    """
    opener = build_opener(proxy, no_proxy)
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            cr = resp.headers.get("Content-Range") or ""
            if "/" in cr:
                tail = cr.rsplit("/", 1)[1].strip()
                if tail.isdigit():
                    return int(tail)
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl and cl.isdigit() else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _total_from_416(exc) -> int | None:
    """从 416 响应里读出服务器上的真实总长度。

    RFC 7233 规定 416 应带 ``Content-Range: bytes */TOTAL``——这是 Range 被拒时
    唯一能问出「文件到底多大」的正规途径。
    """
    headers = getattr(exc, "headers", None)
    cr = (headers.get("Content-Range") or "") if headers else ""
    if "/" in cr:
        tail = cr.rsplit("/", 1)[1].strip()
        if tail.isdigit():
            return int(tail)
    return None


def _download_attempt(opener, url: str, tmp: str, have: int,
                      expect: int | None, timeout: int):
    """跑一次下载尝试，返回服务器声明的总长度（None 表示没声明）。

    只做「一次连接」（读响应 + 追加写 .part），不做重试决策：网络异常原样
    抛给调用方。重试逻辑收在 :func:`download` 一处，网络错误和「传一半就断」
    才能走同一条路。
    """
    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"

    with opener.open(urllib.request.Request(url, headers=headers),
                     timeout=timeout) as resp:
        mode = "ab" if (have and resp.status == 206) else "wb"
        if have and mode == "wb":
            print("  [提示] 服务器未返回 206，忽略已有数据重新下载", file=sys.stderr)
            have = 0

        # 服务器在本次响应里声明的总长度
        total = expect
        cr = resp.headers.get("Content-Range") or ""
        if total is None:
            if "/" in cr and cr.rsplit("/", 1)[1].strip().isdigit():
                total = int(cr.rsplit("/", 1)[1])
            elif resp.headers.get("Content-Length"):
                total = have + int(resp.headers["Content-Length"])

        done = have
        t0 = time.time()
        last = 0.0
        # 边缘节点可能只肯给对象的一部分（对象还在生成 / 缓存没预热）：提前提示，
        # 否则用户只会看到「下到一半就停」，莫名其妙。
        if total and "/" in cr:
            rng_part, _, _tot = cr.partition("/")
            end = rng_part.partition("-")[2]
            if end.isdigit() and int(end) + 1 < total:
                print(f"  [提示] 这个 CDN 节点只返回了前 {human_size(int(end) + 1)}"
                      f"（文件共 {human_size(total)}）——对象可能还在生成或缓存没预热",
                      file=sys.stderr)
        with open(tmp, mode) as fh:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last > 0.3:
                    last = now
                    speed = (done - have) / max(now - t0, 1e-6)
                    if total:
                        print(f"\r  {done * 100 / total:5.1f}%  "
                              f"{human_size(done)}/{human_size(total)}"
                              f"  {human_size(speed)}/s", end="", file=sys.stderr)
                    else:
                        print(f"\r  {human_size(done)}"
                              f"  {human_size(speed)}/s", end="", file=sys.stderr)
        print("\r" + " " * 72 + "\r", end="", file=sys.stderr)
        return total


# 交互超时后默认选的档位标签
DEFAULT_TAG = "720P"
PICK_TIMEOUT = 30.0

# 分享里的转码流是 MPEG-TS，分享名却常写 .mkv；按真实容器纠正扩展名
CONTAINER_EXT = {
    "mpegts": ".ts",
    "matroska": ".mkv",
    "matroska,webm": ".mkv",
    "webm": ".webm",
    "mp4": ".mp4",
    "mov": ".mov",
}


def object_size_from_url(url: str):
    """从直链的 f= 参数取对象的真实字节数（CDN 用它标识对象长度）。"""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    value = (query.get("f") or [""])[0]
    return int(value) if value.isdigit() else None


def available_size(url: str, proxy: str | None = None, no_proxy: bool = False,
                   timeout: int = 30):
    """探测「服务器实际愿意给你多少字节」。失败返回 None。

    这里**不能**用 Range 请求：``Range: bytes=0-0`` 回的 ``Content-Range`` 分母是
    对象的真实大小，即使服务器只有一部分也会照实写总长（实测原画只给
    699 510 782 字节，分母却写 1 214 642 730）。唯一可靠的信号是**不带 Range 的
    普通 GET** 返回的 ``Content-Length``——只有副本完整时它才等于对象大小。

    只读响应头就立刻关掉连接，不会真的把文件下下来。
    """
    opener = build_opener(proxy, no_proxy)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length and length.isdigit() else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _win_readline(timeout: float):
    """Windows 下带超时的单行输入；超时返回 None。"""
    import msvcrt
    deadline = time.time() + timeout
    chars = []
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                return "".join(chars)
            if ch == "\x03":                       # Ctrl+C
                raise KeyboardInterrupt
            if ch == "\x08":                       # 退格
                if chars:
                    chars.pop()
                    print("\b \b", end="", flush=True)
                continue
            if ch in ("\x00", "\xe0"):             # 方向键等功能键：吃掉第二个字节
                msvcrt.getwch()
                continue
            chars.append(ch)
            print(ch, end="", flush=True)
        elif time.time() >= deadline:
            return None
        else:
            time.sleep(0.05)


def _posix_readline(timeout: float):
    import select
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):
        return None
    return sys.stdin.readline() if ready else None


def ask(prompt: str, timeout: float, default: str) -> str:
    """带超时的输入；非交互环境（管道 / 重定向）不等待，直接用默认值。"""
    if not (sys.stdin and sys.stdin.isatty()):
        print(f"{prompt}{default}  # 非交互环境，自动采用默认值 "
              f"（用 --pick 可指定别的档位）", file=sys.stderr)
        return default
    print(prompt, end="", flush=True, file=sys.stderr)
    line = _win_readline(timeout) if os.name == "nt" else _posix_readline(timeout)
    if line is None:
        print(file=sys.stderr)
        print(f"  [{timeout:.0f} 秒内没有输入，自动采用默认值 {default}]", file=sys.stderr)
        return default
    return line.strip() or default


def choose_link(links: list, proxy: str | None = None, no_proxy: bool = False,
                pick=None, timeout: float = PICK_TIMEOUT) -> dict:
    """列出所有档位、标出哪些能完整下载，让用户挑一个。

    每个档位都会先探测服务端**实际**能给的字节数：PikPak 的 CDN 边缘节点可能
    只持有对象的一部分，这种档位下到中途就会断（详见 docs 的 Step 9.5）。探测
    结果写回 ``link["server_size"]``，调用方不用再发一次请求。
    """
    total = len(links)
    default_index = next(
        (i for i, link in enumerate(links, 1)
         if (link.get("resolution") or "").upper() == DEFAULT_TAG), 1)

    if pick is not None:
        if not (str(pick).isdigit() and 1 <= int(pick) <= total):
            raise PikPakError(f"--pick 必须是 1..{total} 之间的整数，收到 {pick!r}")
        chosen = links[int(pick) - 1]
        chosen["server_size"] = available_size(chosen["url"], proxy, no_proxy)
        return chosen

    print("\n可选档位：", file=sys.stderr)
    for i, link in enumerate(links, 1):
        tag = link.get("resolution") or link.get("category") or "转码"
        if link.get("is_origin"):
            tag = f"原画/{tag}"
        obj = object_size_from_url(link["url"])
        avail = available_size(link["url"], proxy, no_proxy)
        link["server_size"] = avail
        if avail is None or not obj:
            note = "完整度未知"
        elif avail >= obj:
            note = "可完整下载"
        else:
            note = (f"不完整：服务器只有 {human_size(avail)}"
                    f"（{avail * 100 / obj:.0f}%），下到中途会停")
        size_text = f"对象 {human_size(obj)}" if obj else "对象大小未知"
        print(f"  {i}. {tag:<12s} {size_text:<20s} {note}", file=sys.stderr)

    prompt = ("\n提示：PikPak 的 CDN 边缘节点可能只持有对象的一部分，"
              "标「不完整」的档位会在中途停住。\n"
              f"请选择要下载的档位 [1-{total}]，{timeout:.0f} 秒内无输入默认选 "
              f"{DEFAULT_TAG}（第 {default_index} 项）：")
    while True:
        answer = ask(prompt, timeout, str(default_index))
        if answer.isdigit() and 1 <= int(answer) <= total:
            return links[int(answer) - 1]
        print(f"  无效输入，请输入 1..{total} 之间的数字。", file=sys.stderr)


def dest_for(directory: str, item: dict, link: dict) -> str:
    """给下载文件起名。

    非原画档会加上档位后缀（如 ``.720P``），避免和原画同名互相覆盖；容器是
    MPEG-TS 的转码档还会把扩展名纠正成 ``.ts``——分享名常写 ``.mkv``，但转码
    流其实是 TS，按原名保存会误导播放器。
    """
    name = os.path.basename(item.get("name") or item.get("id") or "download")
    stem, ext = os.path.splitext(name)
    if link.get("is_origin"):
        return os.path.join(directory, name)
    tag = (link.get("resolution") or link.get("category") or "transcode")
    tag = tag.replace("/", "_")
    ext = CONTAINER_EXT.get((link.get("container") or "").lower(), ext)
    return os.path.join(directory, f"{stem}.{tag}{ext}")


def download(url: str, dest: str, timeout: int = 30, expect: int | None = None,
             proxy: str | None = None, no_proxy: bool = False, retries: int = 20):
    """流式下载 + 断点续传 + 完整性校验。

    设计要点（旧版会在 CDN 中途断开时静默地打印「完成」）：

      1. 先写 ``dest.part``，字节数和服务器声明的长度一致才改名成 ``dest``；
      2. 每次连接前先看本地已有多少字节，用 ``Range`` 从断点继续；
      3. 只有三种情况算成功：服务器没给长度、实收等于声明、本地正好等于声明。
         **本地比声明还长一律不算成功**——那说明两份数据混在一起了，会把
         ``.part`` 挪到 ``.bad`` 再从零下载；
      4. 网络层面的失败（连接被重置、读超时、响应没读完）计入重试，不崩栈；
      5. PikPak 的 CDN 边缘节点**可能只持有对象的一部分**（对象还在生成、或缓存
         没预热）：它会如实返回 ``Content-Range: bytes 0-N/TOTAL``，也会对超出自己
         副本范围的 ``Range`` 直接回 416。同一个链接会被轮到不同节点，所以**多试
         就有机会碰到完整节点**——重试次数由 ``retries`` 控制（命令行 --dl-retries）；
      6. 403/404 之类永久性失败不空耗重试，直接给出可操作的报错。
    """
    opener = build_opener(proxy, no_proxy)
    tmp = part_path(dest)
    total = expect
    stalled = 0        # 「毫无进展」的次数（无进展响应，或节点拒绝续传）

    def discard_part(why: str) -> None:
        """把对不上的 .part 挪到 .bad（只改名、不删除），保住证据也腾开位置。"""
        bad = tmp + ".bad"
        os.replace(tmp, bad)
        print(f"  {why}。已把旧文件挪到 {bad}，重新从头下载。", file=sys.stderr)

    attempt = 0
    while True:
        have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        if attempt == 0 and have:
            print(f"  本地已有 {human_size(have)}，尝试从断点继续", file=sys.stderr)
        if expect and have > expect:
            discard_part(f"[数据对不上] 本地 .part 有 {human_size(have)}，"
                         f"比服务器声明的 {human_size(expect)} 还长")
            have = 0

        try:
            total = _download_attempt(opener, url, tmp, have, expect, timeout)
            final = os.path.getsize(tmp)
            if total is not None and final > total:
                # 比服务器上的对象还长：两份不是同一份数据的续传
                stalled += 1
                reason = "[数据对不上]"
                discard_part(f"[数据对不上] 本地 {human_size(final)} 比服务器上的"
                             f" {human_size(total)} 还长")
            elif total is None or final == total:
                os.replace(tmp, dest)
                note = "" if total else "  [警告] 服务器未提供长度，无法校验完整性"
                print(f"  完成: {dest} ({human_size(final)}){note}", file=sys.stderr)
                return final
            else:
                reason = (f"[不完整] {human_size(final)}/{human_size(total)}"
                          f"（还差 {human_size(total - final)}）")
                if final == have:
                    stalled += 1
                    reason = (f"[无进展] 服务器对 Range 请求没有返回数据"
                              f"（本地仍是 {human_size(have)}）")
                else:
                    stalled = 0
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                # 本地数据不短于服务器上的对象 → Range 无法满足
                total = _total_from_416(exc) or remote_size(url, proxy, no_proxy)
                have_now = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                if total and have_now == total:
                    os.replace(tmp, dest)
                    print(f"  完成: {dest} ({human_size(have_now)})"
                          f"  [本地 .part 正好等于服务器长度，直接采用]", file=sys.stderr)
                    return have_now
                if total and have_now > total:
                    stalled += 1
                    reason = "[数据对不上]"
                    discard_part(f"[数据对不上] 本地 {human_size(have_now)} 比服务器上的"
                                 f" {human_size(total)} 还长")
                else:
                    # 本地还没下完、节点却拒绝续传：同一链接会被轮到不同节点，
                    # 有的完整、有的只有一部分，所以值得再试几次。
                    stalled += 1
                    known = (f"服务器上共 {human_size(total)}" if total
                             else "而且问不到服务器上的总长度")
                    reason = (f"[节点数据不全] 本地 {human_size(have_now)}、{known}，"
                              f"节点拒绝从这里继续")
            elif 400 <= exc.code < 500:
                # 403/404/410 这类是永久失败，重试 20 次没有意义
                raise TruncatedError(
                    f"服务器返回 HTTP {exc.code} {exc.reason}——直链可能已过期，"
                    f"重新运行本脚本获取新的直链")
            else:
                reason = f"[网络错误] {exc}"        # 5xx：值得重试
        except NETWORK_ERRORS as exc:
            reason = f"[网络错误] {exc}"

        attempt += 1
        if attempt >= retries:
            had = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            hint = ""
            if stalled:
                hint = ("\n  其中多数是「节点数据不全 / 无进展」——这是 PikPak CDN 侧对象不完整\n"
                        "  （对象还在生成、或边缘缓存没预热）造成的，不是脚本或 .part 的问题。\n"
                        "  同一个链接会被轮到不同节点，多试就有机会碰到完整节点：\n"
                        "  加大 --dl-retries 重跑（例如 --dl-retries 200），或过一阵再试、"
                        "换别的画质档。")
            raise TruncatedError(
                f"重试 {retries} 次仍未下完: {tmp} 有 {had} 字节，"
                f"应为 {human_size(total) if total else '未知'}（其中 {stalled} 次毫无进展）。"
                f"{hint}\n  已保留 .part，它是有效前缀，重跑可继续。")
        print(f"  {reason}，第 {attempt} 次重试…", file=sys.stderr)
        time.sleep(min(2 ** min(attempt, 5), 10))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="PikPak 分享链接 -> 直链下载地址",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  %(prog)s https://mypikpak.com/s/xxxx\n"
               "  %(prog)s https://mypikpak.com/s/xxxx -p 提取码 --proxy http://127.0.0.1:7890\n"
               "  %(prog)s https://mypikpak.com/s/xxxx --json -o result.json\n"
               "  %(prog)s https://mypikpak.com/s/xxxx --download ./downloads\n"
               "  %(prog)s https://mypikpak.com/s/xxxx --download ./downloads --pick 3\n"
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
    parser.add_argument("--pick", type=int, metavar="N",
                        help="直接下载第 N 个档位（菜单里的序号），不提问。"
                             "不指定时会在菜单里选：每个档位都标出能否完整下载，"
                             "30 秒内无输入默认选 720P")
    parser.add_argument("--pick-timeout", type=float, default=PICK_TIMEOUT, metavar="秒",
                        help=f"菜单等待输入的时间，超时用默认档位（默认 {PICK_TIMEOUT:.0f} 秒）")
    parser.add_argument("--dl-retries", type=int, default=20, metavar="N",
                        help="下载最大重试次数（默认 20）。PikPak 的 CDN 边缘节点可能只持有"
                             "对象的一部分，同一个链接会被轮到不同节点，调大就能一直换节点试"
                             "（例如 --dl-retries 200）")
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
        failures = 0
        for item in results:
            if not item["download_links"]:
                print(f"\n跳过（没有可用直链）: {item['path']}", file=sys.stderr)
                continue

            print(f"\n{item['path']}", file=sys.stderr)
            try:
                best = choose_link(item["download_links"], args.proxy, args.no_proxy,
                                   pick=args.pick, timeout=args.pick_timeout)
            except PikPakError as exc:
                print(f"  选择档位失败: {exc}", file=sys.stderr)
                failures += 1
                continue

            dest = dest_for(args.download, item, best)

            # 期望大小只信服务器。file_info.size 是**原画**的大小，对转码档并不成立，
            # 拿它当校验标准会把完整的转码文件误判成「截断」。
            expect = remote_size(best["url"], args.proxy, args.no_proxy) \
                or object_size_from_url(best["url"])
            avail = best.get("server_size")
            if expect:
                extra = ""
                if avail is not None:
                    extra = f"，服务器实际可给 {human_size(avail)}（{avail * 100 / expect:.1f}%）"
                print(f"  服务器声明大小: {human_size(expect)}{extra}", file=sys.stderr)
                if avail is not None and avail < expect:
                    print(f"  [警告] 这个档位服务端不完整，下到 {human_size(avail)} 就会停住。"
                          f"已下的部分会留在 .part 里（是有效前缀，以后可续传）；"
                          f"想拿到完整文件请选标「可完整下载」的档位。", file=sys.stderr)
                if best.get("is_origin") and item["size"] and expect != item["size"]:
                    print(f"  [注意] 与 file_info.size（{human_size(item['size'])}）不一致，"
                          f"以服务器声明为准", file=sys.stderr)
            # 已下完的（大小对得上）直接跳过；拿不到服务器大小时，
            # 只有原画才敢用 file_info.size 兜底
            want = expect or (item["size"] if best.get("is_origin") else None)
            if want and os.path.exists(dest) and os.path.getsize(dest) == want:
                print(f"  已下载完成，跳过: {dest}", file=sys.stderr)
                continue

            print(f"下载 {dest}", file=sys.stderr)
            try:
                download(best["url"], dest, expect=expect,
                         proxy=args.proxy, no_proxy=args.no_proxy,
                         retries=args.dl_retries)
            except TruncatedError as exc:
                # 单个文件失败不影响其余文件，最后统一用非 0 退出码表示有失败
                print(f"  下载失败: {exc}", file=sys.stderr)
                failures += 1

        if failures:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
