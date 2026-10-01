# Za7Za8

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
python scripts/pikpak_share_dl.py <链接> --download ./dl      # 顺手下载（会弹出档位菜单）
python scripts/pikpak_share_dl.py <链接> --download ./dl --pick 3
                                                              # 直接下第 3 个档位，不提问
python scripts/pikpak_share_dl.py <链接> --download ./dl --dl-retries 200
                                                              # 下载失败时多换几个 CDN 节点
python scripts/pikpak_share_dl.py <链接> --no-recursive       # 不进子文件夹
python scripts/pikpak_share_dl.py <链接> -v                   # 打印请求细节
python scripts/pikpak_share_dl.py <链接> --no-proxy           # 忽略环境/系统代理，强制直连
```

**下载时会先让你选档位。** 加 `--download` 后，脚本会先探测每个清晰度在服务端**实际**
能拿到多少字节，列出菜单并标出哪些能完整下载：

```
可选档位：
  1. 原画/1080P     对象 1.13 GiB          不完整：服务器只有 667.11 MiB（58%），下到中途会停
  2. 480P         对象 126.86 MiB        可完整下载
  3. 720P         对象 210.08 MiB        可完整下载
  4. 1080P        对象 382.48 MiB        不完整：服务器只有 240.37 MiB（63%），下到中途会停

请选择要下载的档位 [1-4]，30 秒内无输入默认选 720P（第 3 项）：
```

- **30 秒内没输入就默认选 720P**（低一档、体积小、兼容性好；菜单里没有 720P 时选第 1 项）。
- 探测用不带 `Range` 的普通 GET，只读 `Content-Length` 就断开——因为 `Range: bytes=0-0`
  回的是对象的**声明**总长，即使服务器只有一部分也照实写满，没法用来判断完整度。
- 下载非原画档时文件名会加档位后缀；容器是 MPEG-TS 的转码档自动存成 `.ts`
  （分享名写的 `.mkv` 是错的，直接播 `.ts` 即可）。
- 管道 / 重定向下不等待，直接用默认档位；脚本里想指定就加 `--pick N`。

**下载不完整怎么办？** 标「不完整」的档位是**服务端那个对象本身就只有一部分字节**
（实测元数据里 `phase` 写着 `PHASE_TYPE_COMPLETE`，服务器却只能给出前 58%），
不是脚本的错，也不是「缓存没预热」——实测这种截断点可以稳定存在好几天，
不要指望靠重试变出来。所以：**优先选标「可完整下载」的档位**；实在要原画，
可先去下能拿到的部分（`.part` 是有效前缀，以后可续传），或过一阵再看长度会不会变长。
详见 [`docs/pikpak-share-to-direct-link.md`](docs/pikpak-share-to-direct-link.md) 的 Step 9。
详见 [`docs/pikpak-share-to-direct-link.md`](docs/pikpak-share-to-direct-link.md) 的 Step 9。

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
  取直链需要境外出口。直链本身（`dl-*.mypikpak.com`）不做地区限制，但实测大陆
  直连有时被限速到 0.1–0.3 MiB/s，**走代理能有 1.4–4.2 MiB/s**——直连下得慢时
  不妨把 `--proxy` 也加上。
- **直链有效期约 16 小时**（以响应里的 `expire` 字段为准），过期重跑一次即可。
- **下载要留意完整性**：服务端对象可能只有一部分（元数据里 `phase` 写着
  `PHASE_TYPE_COMPLETE`，服务器却只能给出前 58%），表现为「每次停在同一个大小
  且不报错」或「续传报 416」。用 `--download` 时脚本会预先探测并标出哪些档位能下完，
  选标「可完整下载」的即可。脚本只在校验通过后才把 `.part` 改名成成品，
  不会留下似是而非的半成品。
- 直链是带签名的临时凭据，**不要公开传播**。
- 脚本只调用匿名可访问的公开分享接口，不涉及登录、付费或权限绕过。
  请自行确认使用的合规性，以及分享内容本身的版权问题。
