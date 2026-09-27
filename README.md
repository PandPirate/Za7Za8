# others

一个杂七杂八的仓库：平时随手写的小工具、AI 相关的实验与笔记，以及把折腾过程
整理出来的文档。都是按需写的，不追求通用，也不保证长期维护。

## 目录结构

```
.
├── docs/      文档：原理、方法、排查
└── scripts/   可运行的脚本
```

## 脚本用法

### `scripts/pikpak_share_dl.py`

把 PikPak 的分享 / 播放链接解析成可直接下载的 CDN 直链，**不需要登录账号**。
纯标准库，无第三方依赖，Python 3.8+。

```bash
# 最简：分享链接 -> 直链
python scripts/pikpak_share_dl.py https://mypikpak.com/s/VP2DqofIa881OLWRaKYH0OYco2

# 大陆 IP 必须走境外代理（PikPak 对大陆关闭了分享接口）
python scripts/pikpak_share_dl.py <链接> --proxy http://127.0.0.1:7890

# SOCKS 代理（需要 pip install PySocks）
python scripts/pikpak_share_dl.py <链接> --proxy socks5h://127.0.0.1:1080

# 其它常用参数
python scripts/pikpak_share_dl.py <链接> -p 提取码            # 带提取码的分享
python scripts/pikpak_share_dl.py <链接> --json -o out.json   # 输出 JSON 到文件
python scripts/pikpak_share_dl.py <链接> --download ./dl      # 顺手下载
python scripts/pikpak_share_dl.py <链接> --no-recursive       # 不进子文件夹
python scripts/pikpak_share_dl.py <链接> -v                   # 打印请求细节
```

输入形式都认：

| 输入 | 说明 |
| --- | --- |
| `https://mypikpak.com/s/<share_id>` | 标准分享链接 |
| `https://mypikpak.com/s/<share_id>?pwd=xxxx` | 带提取码 |
| `https://mypikpak.com/s/<share_id>/<file_id>` | 指向具体文件的深链 |
| `<share_id>` | 裸 ID |

输出会在每个文件下列出所有清晰度，**原画排在第一位**，并带上过期时间和直链 URL。

### `scripts/pikpak_minimal.py`

上面那个脚本的最小版本：76 行，去掉了命令行参数、文件夹递归、令牌重试和下载，
只保留核心请求链路，用来对照文档逐行阅读。

```bash
# 改脚本顶部的 SHARE_LINK（需要代理就同时改 PROXY），然后直接运行
python scripts/pikpak_minimal.py
```

## 文档

- [`docs/pikpak-share-to-direct-link.md`](docs/pikpak-share-to-direct-link.md)
  —— 从零开始讲「PikPak 分享链接 → 直链」的原理与实现，包含：

  - HTTP、SPA、预签名 URL、内容哈希、风控验证码、地理围栏等基础概念
  - 怎么自己从压缩过的前端 JS 里找出接口路径和签名算法
  - Step 1~9 逐步拆解，每步都是「原理 → 真实请求与响应 → 脚本代码」
  - 排查决策树、字段速查表、常见报错对照表
  - 每条结论都标注了是「实测」还是「读代码推断」

## 注意事项

- **地区限制**：PikPak 的分享接口对大陆 IP 返回 `share_status=PROHIBITED`，
  取直链需要境外出口。但直链本身（`dl-*.mypikpak.com`）不做地区限制，
  拿到后在大陆可以直接满速下载。
- **直链有效期约 16 小时**（以响应里的 `expire` 字段为准），过期重跑一次即可。
- 直链是带签名的临时凭据，**不要公开传播**。
- 脚本只调用匿名可访问的公开分享接口，不涉及登录、付费或权限绕过。
  请自行确认使用的合规性，以及分享内容本身的版权问题。
