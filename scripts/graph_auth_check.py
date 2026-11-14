#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph_auth_check.py —— Graph 认证自检：拿票、验签、并实调 /me 看能不能用。

零依赖，只用标准库。要分清两件事：

    id_token      一定是 JWT，用来确认「登录的是谁」，可离线验签
    access_token  组织账户是 JWT；个人账户(MSA)常是 EwB… 不透明票，
                  **但它照样能调 Graph**——能不能用只由 Graph 说了算

所以本脚本最终以「Graph /me 返回 200」为成功判据，而不是「是不是 JWT」。

取票 / 换票 / token 缓存全部复用 `lib/graph_calendar.py`——和 `send_digest.py --graph-login`
是同一套实现、同一个缓存文件，不会两边漂移。这里只保留诊断独有能力：JWT 解码验签、
scope 探针、把结论说清楚。

用法：

    python scripts/graph_auth_check.py --client-id <应用ID>
    python scripts/graph_auth_check.py --client-id <应用ID> --probe-scopes
    python scripts/graph_auth_check.py --reuse              # 复用已有 token 缓存
    python scripts/graph_auth_check.py --token eyJ0eXAi...  # 只检查一枚现成的票

client_id 也可以走环境变量 MAILER_GRAPH_CLIENT_ID（和 send_digest 共用）。
退出码：0 = Graph 接受这枚票；1 = 被拒；2 = 参数 / 网络 / 授权问题。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time

import _bootstrap  # noqa: F401  （把 lib/ 放进 sys.path）

import graph_calendar as gc

# common 的这组公钥同时覆盖 AAD 各租户和 MSA（9188040d），不必按 iss 分头取
JWKS_URL = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
MSA_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
GRAPH_AUD = "https://graph.microsoft.com"

# 完整 URI 是关键：个人账户下短名可能被 MSA 侧解析成「LIVE 资源」，回一个不透明票
DEFAULT_SCOPES = (
    "openid profile email offline_access "
    "https://graph.microsoft.com/User.Read "
    "https://graph.microsoft.com/Calendars.ReadWrite"
)

# 探针：同一枚 refresh_token 换不同写法，看哪种才回 JWT
PROBES = [
    ("完整 URI + 短名混写", "https://graph.microsoft.com/User.Read offline_access"),
    ("只有短名", "User.Read offline_access"),
    ("只有 OIDC，不带资源", "openid profile email offline_access"),
]

_KEYS_CACHE: dict = {}


# ---------------------------------------------------------------- 基础工具


def b64u_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def b64u_json(segment: str) -> dict:
    return json.loads(b64u_decode(segment).decode("utf-8"))


def section(title: str) -> None:
    print()
    print("─" * 62)
    print(title)
    print("─" * 62)


def short(value, width: int = 46) -> str:
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


# ---------------------------------------------------------------- JWT 验签（纯标准库 RS256）


def rsa_verify_rs256(signing_input: bytes, signature: bytes, jwk: dict) -> bool:
    """按 PKCS#1 v1.5 + SHA-256 验 RS256 签名。只做验证，不需要第三方库。"""
    n = int.from_bytes(b64u_decode(jwk["n"]), "big")
    e = int.from_bytes(b64u_decode(jwk["e"]), "big")
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    # EMSA-PKCS1-v1_5：0x00 0x01 FF..FF 0x00 || DigestInfo(SHA-256)
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420")
    digest_info += hashlib.sha256(signing_input).digest()
    if size < len(digest_info) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    return hmac.compare_digest(pow(s, e, n).to_bytes(size, "big"), expected)


def _keys() -> list:
    if "v2" not in _KEYS_CACHE:
        body = gc.fetch_json(JWKS_URL)
        if "keys" not in body:
            raise SystemExit("公钥响应里没有 keys：{}".format(short(body, 200)))
        _KEYS_CACHE["v2"] = body["keys"]
    return _KEYS_CACHE["v2"]


def verify_signature(parts: list, header: dict) -> tuple[bool, str]:
    alg = header.get("alg", "")
    if alg != "RS256":
        return False, "不支持的 alg={}（微软只会用 RS256）".format(alg)
    kid = header.get("kid", "")
    keys = _keys()
    matches = [k for k in keys if k.get("kid") == kid] or [
        k for k in keys if k.get("kty") == "RSA" and k.get("use", "sig") == "sig"
    ]
    if not matches:
        return False, "公钥集里没有 kid={}".format(kid or "(空)")
    signing_input = (parts[0] + "." + parts[1]).encode("ascii")
    signature = b64u_decode(parts[2])
    for key in matches:
        try:
            if rsa_verify_rs256(signing_input, signature, key):
                return True, "kid={}".format(key.get("kid", ""))
        except Exception:  # 单个 key 解析失败不影响其它的
            continue
    return False, "签名对不上（公钥可能已轮换，重跑即可）"


def describe(label: str, token: str, client_id: str = "") -> tuple[dict | None, bool]:
    """打印一枚票的形状 + 声明 + 验签结果；返回 (claims, 验签是否通过)。"""
    print()
    print("[{}] {} 字符，{} 段".format(label, len(token), token.count(".") + 1))
    parts = token.split(".")
    if len(parts) != 3:
        if token.startswith("Ew"):
            print("  · 不透明票（前缀 {}）——个人账户的常态，不是错误".format(short(token, 12)))
            print("    Graph 接受这种票；只是没法当 JWT 解析/验签。能不能用问 Graph。")
        else:
            print("  · 不是 JWT（前缀 {}），能否使用要问资源服务器".format(short(token, 12)))
        return None, False

    try:
        header, claims = b64u_json(parts[0]), b64u_json(parts[1])
    except Exception:
        print("  ✗ 三段结构但不是合法 JWT（base64/JSON 解不开）")
        return None, False

    print("  头部：alg={} kid={} typ={}".format(
        header.get("alg", "?"), short(header.get("kid", ""), 24), header.get("typ", "")
    ))
    for key in ("aud", "iss", "scp", "roles", "tid", "azp", "ver", "acr"):
        if claims.get(key):
            print("  {:<19}{}".format(key + ":", short(claims[key], 96)))
    for key in ("preferred_username", "upn", "email", "name", "oid", "sub"):
        if claims.get(key):
            print("  {:<19}{}".format(key + ":", short(claims[key], 96)))

    try:
        ok, detail = verify_signature(parts, header)
    except Exception as exc:  # 签名段 base64 坏了之类
        ok, detail = False, "验签出错：{}".format(exc)
    print("  验签：{}  {}".format("✓ 通过" if ok else "✗ 失败", detail))

    now = int(time.time())
    left = int(claims.get("exp", 0)) - now
    print("  有效期：{}{}".format(
        "还剩 {} 分钟".format(left // 60) if left > 0 else "已过期 {} 分钟".format(-left // 60),
        "" if claims.get("exp") else "（无 exp）",
    ))
    if claims.get("nbf") and claims["nbf"] > now + 120:
        print("  ✗ nbf 在未来，票还不能用")
    if client_id:
        if label == "id_token" and claims.get("aud") != client_id:
            print("  ✗ aud 不等于 client_id，这枚 id_token 不是发给你的应用")
        if label == "access_token" and claims.get("aud") != GRAPH_AUD:
            print("  ! aud 不是 {}，这枚 access_token 调不了 Graph".format(GRAPH_AUD))
    if claims.get("tid") == MSA_TENANT:
        print("  · tid={} → 这是个人账户（MSA）的租户，正常".format(MSA_TENANT[:8] + "…"))
    return claims, ok


# ---------------------------------------------------------------- 错误说明


def explain(exc) -> None:
    """把 OAuth / Graph 的报错翻译成「下一步该改什么」。"""
    code = getattr(exc, "code", "") or ""
    print("✗ {}".format(exc))
    if code:
        print("  error: {}".format(code))
    hints = {
        "AADSTS700016": "client_id 不对，或应用不在该租户里",
        "AADSTS7000218": "应用没开「允许公共客户端流」",
        "AADSTS65001": "权限没加在「Microsoft Graph → 委托的权限」下，或浏览器里点了拒绝",
        "AADSTS50059": "client_id 抄错，或这个应用不在该云里",
        "AADSTS90002": "租户不存在，--tenant 写错了",
        "AADSTS50020": "账户类型不是「组织 + 个人」，个人账户进不去",
        "AADSTS500200": "应用只收组织账户，个人账户被拒",
        "invalid_grant": "refresh_token 过期或被撤销，重新登录一次",
        "invalid_scope": "scope 写法不被接受，试试完整 URI",
        "unauthorized": "Graph 明确拒绝这枚票：查 client_id / API 权限 / signInAudience",
    }
    text = code + " " + str(exc)
    for marker, hint in hints.items():
        if marker in text:
            print("  可能原因：{}".format(hint))
            break


# ---------------------------------------------------------------- scope 探针


def probe_scopes(client_id: str, tenant: str, refresh_token: str) -> dict:
    section("④ scope 探针：同一枚 refresh_token，换写法看哪种回 JWT")
    if not refresh_token:
        print("  没有 refresh_token，跳过（登录时没要 offline_access？）")
        return {}
    verdict = {}
    for name, scope in PROBES:
        try:
            body = gc.exchange_refresh_token(client_id, tenant, scope, refresh_token)
        except gc.GraphError as exc:
            print("  ✗ {:<20} {}".format(name, short(getattr(exc, "code", "") or exc, 60)))
            verdict[name] = "失败"
            continue
        access = body.get("access_token", "")
        is_jwt = access.count(".") == 2
        aud = ""
        if is_jwt:
            try:
                aud = b64u_json(access.split(".")[1]).get("aud", "")
            except Exception:
                aud = "?"
        print("  {} {:<20} {}  aud={}".format(
            "✓" if is_jwt else "✗", name, "JWT" if is_jwt else "不透明票", aud or "-"
        ))
        verdict[name] = "JWT" if is_jwt else "不透明票"
    if verdict.get(PROBES[0][0]) == "JWT":
        print("\n  完整 URI 写法回的是 JWT；不过个人账户拿到不透明票也正常，看 Graph 认不认。")
    return verdict


# ---------------------------------------------------------------- Graph


def call_me(access_token: str) -> bool:
    """这枚票 Graph 到底认不认——这才是真正的判据，跟它是不是 JWT 无关。"""
    section("⑤ 实调 Graph /me")
    try:
        body = gc.me(access_token)
    except gc.GraphError as exc:
        print("  ✗ {}".format(exc))
        if getattr(exc, "code", "") == "unauthorized":
            print("  401：Graph 明确拒绝这枚票，回去查 client_id / API 权限 / signInAudience")
        return False
    print("  {} / {}".format(body.get("displayName", "?"), body.get("userPrincipalName", "?")))
    print("  ✓ 这枚票能用，日历就写到这个账号名下")
    return True


# ---------------------------------------------------------------- 两种入口


def run_login(args, creds, scope: str, token: dict | None = None) -> int:
    if token is None:
        try:
            token = gc.device_login(creds, scope=scope)
        except gc.GraphError as exc:
            explain(exc)
            return 2
        if not token:
            return 2

    section("② 换到的票")
    print("  token_type   : {}".format(token.get("token_type", "?")))
    print("  scope        : {}".format(short(token.get("scope", "-"), 120)))
    print("  有效期       : {} 秒".format(token.get("expires_in")))
    print("  refresh_token: {} 字符".format(len(token.get("refresh_token", ""))))

    section("③ 解码与验签")
    claims, verified = {}, {}
    for label in ("id_token", "access_token"):
        value = token.get(label)
        if value:
            claims[label], verified[label] = describe(label, value, creds.client_id)

    access_raw = token.get("access_token", "")
    access_jwt = bool(claims.get("access_token"))
    id_jwt = bool(claims.get("id_token"))

    # 探针必须排在提前 return 之前：access_token 不透明时正是要看它
    if args.probe_scopes:
        verdict = probe_scopes(creds.client_id, creds.tenant, token.get("refresh_token", ""))
        if verdict and all(value != "JWT" for value in verdict.values()):
            print("\n  · 三种写法都不是 JWT —— 个人账户下这很正常，不代表出错。")
            print("    判据是下一节：Graph 认不认这枚票。")

    if not access_raw:
        print("\n✗ 响应里根本没有 access_token")
        return 1

    if args.no_call:
        section("结论")
        if access_jwt and verified.get("access_token"):
            print("  ✓ access_token 是 JWT 且验签通过（--no-call，未实调 Graph）")
            return 0
        print("  · 不透明票，没实调 Graph 就无法下结论；去掉 --no-call 再跑")
        return 1

    ok = call_me(access_raw)
    section("结论")
    if ok:
        print("  ✓ Graph 接受这枚 access_token，直接拿它读写日历就行")
        print("    票是不是 JWT 无所谓；身份以 /me 的返回或 id_token 为准")
        if access_jwt and not verified.get("access_token"):
            print("  ! 它是 JWT 但验签没过，别当登录凭证用（调 Graph 不影响）")
        return 0
    print("  ✗ Graph 拒绝这枚票，回去查 client_id / API 权限 / signInAudience")
    return 1


def run_token(args, token: str) -> int:
    section("① 解码与验签")
    describe("token", token, args.client_id or "")
    if args.no_call:
        return 1 if token.count(".") != 2 else 0
    return 0 if call_me(token) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="设备代码流拿微软 JWT，并解码 / 验签 / 实调 Graph。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  python scripts/graph_auth_check.py --client-id 00000000-0000-0000-0000-000000000000\n"
        "  python scripts/graph_auth_check.py --client-id <id> --probe-scopes\n"
        "  python scripts/graph_auth_check.py --reuse\n"
        "  python scripts/graph_auth_check.py --token eyJ0eXAi...\n",
    )
    parser.add_argument("--client-id", default=os.environ.get("MAILER_GRAPH_CLIENT_ID", ""),
                        help="应用(客户端) ID，默认读 MAILER_GRAPH_CLIENT_ID")
    parser.add_argument("--tenant", default="common",
                        help="common（默认）/ consumers / 具体租户 ID")
    parser.add_argument("--scope", action="append", metavar="SCOPE",
                        help="可重复；默认是 Graph 完整 URI + OIDC")
    parser.add_argument("--token", metavar="JWT", help="不解码登录，只检查一枚现成的票")
    parser.add_argument("--probe-scopes", action="store_true",
                        help="登录后用 refresh_token 试各种 scope 写法")
    parser.add_argument("--no-call", action="store_true", help="不实调 Graph /me")
    parser.add_argument("--force-call", action="store_true",
                        help="（已废弃，现在默认就会调 Graph；留着只为兼容旧命令）")
    parser.add_argument("--reuse", action="store_true",
                        help="复用缓存的 refresh_token，不重新走设备码")
    parser.add_argument("--proxy", metavar="URL", help="如 http://127.0.0.1:7890")
    return parser


def main(argv=None) -> int:
    # Windows 重定向到管道/文件时默认 GBK：✓ ✗ 之类会炸，统一成 UTF-8
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if args.proxy:
        gc.set_proxy(args.proxy)

    if args.token:
        return run_token(args, args.token.strip())

    scope = " ".join(args.scope) if args.scope else DEFAULT_SCOPES

    if args.reuse:
        cache = gc.load_token_cache()
        if not cache.get("refresh_token"):
            print("没有可复用的缓存（{}），先跑一次 --graph-login".format(gc.token_path()))
            return 2
        client_id = (args.client_id or cache.get("client_id", "")).strip()
        tenant = args.tenant if args.tenant != "common" else cache.get("tenant", "common")
        if not client_id:
            print("缓存里没有 client_id，请显式传 --client-id")
            return 2
        creds = gc.Creds(client_id, tenant, gc.token_path())
        try:
            token = gc.exchange_refresh_token(client_id, tenant, scope, cache["refresh_token"])
        except gc.GraphError as exc:
            explain(exc)
            return 2
        gc.store_token(creds, token)
        return run_login(args, creds, scope, token=token or None)

    client_id = (args.client_id or "").strip()
    if not client_id:
        print("缺少 --client-id（或环境变量 MAILER_GRAPH_CLIENT_ID）")
        return 2
    return run_login(args, gc.Creds(client_id, args.tenant, gc.token_path()), scope)


if __name__ == "__main__":
    sys.exit(main())
