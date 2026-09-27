#!/usr/bin/env python3
"""PikPak 分享链接 -> 直链：最小可运行版本

配套 docs/pikpak-share-to-direct-link.md 的 Step 1~9，去掉了命令行参数、
文件夹递归、代理重试、下载等外围功能，只保留最短链路。
大陆 IP 调 API 会被地区限制，此时把下面的 PROXY 填成你的代理地址。
"""

import hashlib, json, random, time, urllib.error, urllib.parse, urllib.request

SHARE_LINK = "https://mypikpak.com/s/VP2DqofIa881OLWRaKYH0OYco2"
PROXY = None  # 例如 "http://127.0.0.1:7890"

CLIENT_ID, PACKAGE, VERSION = "YUMx5nI8ZU8Ap8pm", "drive.mypikpak.com", "undefined"
SALTS = ["fyZ4+p77W1U4zcWBUwefAIFhFxvADWtT1wzolCxhg9q7etmGUjXr", "uSUX02HYJ1IkyLdhINEFcCf7l2",
         "iWt97bqD/qvjIaPXB2Ja5rsBWtQtBZZmaHH2rMR41", "3binT1s/5a1pu3fGsN",
         "8YCCU+AIr7pg+yd7CkQEY16lDMwi8Rh4WNp5", "DYS3StqnAEKdGddRP8CJrxUSFh",
         "crquW+4", "ryKqvW9B9hly+JAymXCIfag5Z", "Hr08T/NDTX1oSJfHk90c", "i"]

# Step 2+3：设备号与时间戳，再跑 10 轮加盐 MD5 得到 captcha_sign
DEVICE = "".join(random.choice("0123456789abcdef") for _ in range(32))
STAMP = str(int(time.time() * 1000))
sign = CLIENT_ID + VERSION + PACKAGE + DEVICE + STAMP
for salt in SALTS:
    sign = hashlib.md5((sign + salt).encode()).hexdigest()
CAPTCHA_SIGN = "1." + sign

HEADERS = {"x-device-id": DEVICE, "x-client-id": CLIENT_ID, "Accept-Language": "zh-CN",
           "Origin": "https://mypikpak.com", "Referer": "https://mypikpak.com/",
           "User-Agent": "Mozilla/5.0"}
proxy = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}) if PROXY else None
opener = urllib.request.build_opener(*([proxy] if proxy else []))


def call(url, action, body=None):
    """发请求；action 非空时先换一枚 captcha_token（Step 4）。"""
    headers = dict(HEADERS)
    if action:
        payload = {"client_id": CLIENT_ID, "action": action, "device_id": DEVICE,
                   "meta": {"captcha_sign": CAPTCHA_SIGN, "client_version": VERSION,
                            "package_name": PACKAGE, "user_id": "", "timestamp": STAMP}}
        req = urllib.request.Request("https://user.mypikpak.com/v1/shield/captcha/init",
                                     data=json.dumps(payload).encode(),
                                     headers={**headers, "Content-Type": "application/json"})
        headers["x-captcha-token"] = json.load(opener.open(req))["captcha_token"]
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers)  # 有 data 即 POST
    try:
        return json.load(opener.open(req))
    except urllib.error.HTTPError as exc:  # 业务错误也用 4xx 返回 JSON
        return json.load(exc)


API = "https://api-drive.mypikpak.com/drive/v1"
share_id = SHARE_LINK.split("/s/")[1].split("?")[0]      # Step 1

# Step 5：读分享信息，拿到文件列表和 pass_code_token
q = urllib.parse.urlencode({"share_id": share_id, "pass_code": "", "limit": "100"})
meta = call(f"{API}/share?{q}", "GET:/drive/v1/share")
if meta.get("share_status") != "OK":
    raise SystemExit(f"分享不可用：{meta.get('share_status')} {meta.get('share_status_text')}")

# Step 6+7：逐个文件问详情（只处理根目录，不递归文件夹）
for item in meta["files"]:
    if item.get("kind") == "drive#folder":
        continue
    q = urllib.parse.urlencode({"share_id": share_id, "file_id": item["id"],
                                "pass_code_token": meta.get("pass_code_token", "")})
    info = call(f"{API}/share/file_info?{q}", "GET:/drive/v1/share/file_info")["file_info"]

    # Step 8：原画排最前，同档按 priority 降序
    medias = sorted(info.get("medias") or [],
                    key=lambda m: (not m.get("is_origin"), -m.get("priority", 0)))
    print(f"\n{info['name']}  ({int(info['size']) / 1048576:.1f} MiB)")
    for m in medias:
        print(f"  [{m.get('resolution_name') or 'default'}] {m['link']['url']}")
