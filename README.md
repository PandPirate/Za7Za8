# Za7Za8

一个杂七杂八的仓库：平时随手写的小工具、AI 相关的实验与笔记，以及把折腾过程
整理出来的文档。都是按需写的，不追求通用，也不保证长期维护。

## 目录结构

```
.
├── docs/       文档：原理、方法、排查
├── lib/        可 import 的库：mail/（发邮件）+ graph_calendar.py（写日历）
├── scripts/    可执行的入口：只做参数与转交，不藏实现
├── downloads/  脚本产物：下载的文件、邮件预览、.ics（不入库）
└── state/      机器状态与凭据：Graph token、诊断缓存（不入库）
```

目录约定：**`scripts/` 里的文件都能直接跑，`lib/` 里的东西都是被 import 的**。
两个领域各自独立：

| 单元 | 形态 | 为什么 |
|---|---|---|
| `lib/mail/` | 目录包 | 发邮件真的要多个文件：插件协议、版面、SMTP 配置、CLI，还要放 plugins/ |
| `lib/graph_calendar.py` | 单文件 | 写日历就一件事（认证 + Graph + 同步策略），合在一个文件里读着更顺 |
| `lib/paths.py`、`lib/constants.py` | 单文件 | 两个领域都要用的小东西 |

`mail` 的框架层（`core` / `template` / `config`）不知道日历的存在；只有 `cli` 为了打
“没配 Graph”那句提示、和插件为了写日历，才 `import graph_calendar`。`downloads/` 可以
随便删，`state/` 删了就要重新登录。

`pyrightconfig.json` 不是给运行用的：`lib/` 只在**运行时**由 `scripts/_bootstrap.py`
塞进 `sys.path`，静态分析看不到它。这份配置声明了 `extraPaths`，编辑器里
`import graph_calendar` 才能解析并跳转到定义。

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

### `scripts/cn_holiday_ics.py`

把国务院办公厅公布的法定节假日安排生成 `.ics` 日历，导入 Outlook / Google 日历 /
Apple 日历后，月视图里一眼能看出哪天放假、哪个周末要补班。纯标准库，Python 3.8+。

```bash
python scripts/cn_holiday_ics.py                    # 2027 年，输出 downloads/2027中国节假日.ics
python scripts/cn_holiday_ics.py -o 2027.ics        # 换个输出路径
python scripts/cn_holiday_ics.py --holidays-only    # 只要放假日，不要调休上班日
python scripts/cn_holiday_ics.py --no-alarms        # 补班日不加提醒
```

每个放假日一条全天事件（标「空闲」），每个调休上班日也有一条（标「忙」，并在前一天
9 点提醒）。数据是手工维护的：每年 11 月通知出来后，把 `YEAR_DATA` 里新一年的
`blocks` / `workdays` 照通知原文抄进去就行。

生成前会用通知里写明的「日期（星期）」自检一遍，抄错日期、星期对不上都会直接报错，
所以改完数据跑一次就知道有没有抄错。

### `scripts/send_digest.py`

可插拔的摘要邮件框架：**插件出内容，框架管版式和投递**。一个插件挂掉不会影响别的
插件，也不会发出一封半成品——没内容的区块直接不出现。

```bash
python scripts/send_digest.py --list                       # 有哪些插件
python scripts/send_digest.py --dry-run                    # 生成预览（.eml + .html），不发信
python scripts/send_digest.py --plugin premier_league      # 只跑某个插件（可重复）
```

发信前配 SMTP，然后去掉 `--dry-run`：

```bash
export MAILER_SMTP_HOST=smtp.qq.com      # Windows: set / $env:
export MAILER_SMTP_PORT=587              # 465 用 MAILER_SMTP_TLS=ssl
export MAILER_SMTP_USER=you@qq.com
export MAILER_SMTP_PASS=授权码
export MAILER_TO=me@example.com
python scripts/send_digest.py            # 真发
```

不想每次 export，就写一个 JSON 给 `--config`（字段名和上面的环境变量一一对应）。
这个文件里有授权码，**放 `state/`**（已 gitignore）：

```bash
python scripts/send_digest.py --config state/mail.json
```

自带的四个插件：

| 插件 | 说明 |
| --- | --- |
| `premier_league` | 英超上一轮赛果、积分榜、下一轮赛程、看点，并把赛程写进 Outlook 日历 |
| `champions_league` | 欧冠近期赛果、联赛阶段积分榜、下一批赛程、看点，并把赛程写进 Outlook 日历 |
| `habit` | 21 天习惯养成：今日待办，回复邮件完成/说明原因；`--plugin habit` 才跑 |
| `example` | 插件契约示例，`--plugin example` 才跑 |

`champions_league` 跟着欧冠赛程走：窗口内既没有刚结束的比赛、也没有即将开始的比赛时，
整个区块不出现（不拿上赛季的数据凑数）。欧冠用独立的 `Automation/欧冠赛程 2026-27`
日历，去重和迁移规则与英超相同。

邮件里只有数据：赛果是比分，赛程是北京时间，看点是自动算出来的（进球大战、焦点战）；
不带战报、集锦、附件。取不到实时数据时这一块整体不出现，不拿旧比分凑数。

**日历只走 Microsoft Graph。** 同一批日程写进 Outlook 的 `Automation/英超赛程 2026-27`
（`Automation` 是日历组，相当于目录）：每场 2 小时、标「空闲」、赛前 30 分钟提醒，
按 `subject + 开始时间` 去重：重跑只更新不重复；日历里若已有同键副本，只保留一条并
删掉多余的，也不碰别的日程。

日历组不存在会自动创建（默认 `Automation`，可用 `MAILER_CALENDAR_GROUP` 改）。如果同名
日历已经在别的组里，Graph 没有“移动日历”接口，工具会把旧日历先改名、在目标组用原名
新建、复制全部事件，再删旧日历；中途失败会回滚，事件不会丢。

```bash
python scripts/send_digest.py --dry-run                      # 预览 + 演练日历（不落库）
python scripts/send_digest.py --dry-run --apply-calendar     # 预览 + 真写日历
python scripts/send_digest.py --calendar none                # 只发内容，不碰日历
```

`--dry-run` 默认只演练（正文里写「日历：演练，将同步 …」），加 `--apply-calendar` 才真写；
没配 Graph 时 `auto` 会跳过日历，并在日志里说明原因。

#### 配置 Graph（一次性）

需要一个**允许个人账户**的 Azure 应用（拿客户端 ID），再在跑任务的机器上 `--graph-login`
一次，token 就缓存在本地。完整步骤、报错对照表在
[`docs/outlook-graph-calendar.md`](docs/outlook-graph-calendar.md)。

```bash
export MAILER_GRAPH_CLIENT_ID=<客户端 ID>
python scripts/send_digest.py --graph-login        # 浏览器输码、用要写日历的账号同意
python scripts/send_digest.py --graph-whoami       # 确认 token 属于哪个账号
python scripts/send_digest.py --dry-run --apply-calendar
```

token 存在 `state/.graph_token.json`（已 gitignore，**等于密码，别外传**），过期会用
refresh_token 自动续；`--graph-logout` 删掉它。

**加插件**就是往 `lib/mail/plugins/` 里丢一个模块，暴露 `PLUGIN` 对象：

```python
from ..core import Section

class MyPlugin:
    name = "my_plugin"
    title = "我的区块"
    description = "一句话说明发什么"
    enabled_by_default = False      # True 就默认进邮件

    def build(self, ctx):           # ctx.now / ctx.http_json
        return Section(key=self.name, title=self.title, html="<p>hi</p>", text="hi")

PLUGIN = MyPlugin()
```

框架靠 `pkgutil` 自动发现，不用改注册表。插件里想写日历，就照英超插件那样组
`CalendarEvent` 交给 `graph_calendar.sync()`。想定时发就挂 cron / 计划任务；
写日历是幂等的（按 `subject + 开始时间` 去重，并清理已有的同键副本），
重复跑只更新不重复。

### `scripts/send_premier_league.py`

`send_digest.py` 的单插件快捷方式：只发英超快报，并把下一轮赛程写进 Outlook 日历。
等价于 `send_digest.py --plugin premier_league --calendar graph`，其余参数原样转交。

```bash
python scripts/send_premier_league.py --dry-run   # 预览邮件 + 演练日历（不落库）
python scripts/send_premier_league.py             # 真发信 + 真写日历
python scripts/send_premier_league.py --to me@example.com
python scripts/send_premier_league.py --dry-run --apply-calendar   # 预览的同时真写日历
```

自己写了 `--plugin` / `--calendar` 就按自己的来（例如 `--calendar none` 只发信）；
`--graph-login`、`--config` 这些也都能直接传给它。SMTP 与 Graph 的首次配置见上一节。
写日历是幂等的：按 `subject + 开始时间` 去重，重复跑只更新，日历里已有的同键副本
会被清理，只用一条。

欧冠没有单独入口，用 `python scripts/send_digest.py --plugin champions_league` 即可；
不加 `--plugin` 时两个插件都会跑，欧冠没比赛会自动跳过。

### `scripts/send_leagues.sh`

一条命令发两封独立邮件：英超、欧冠。脚本先设置 `MAILER_GRAPH_CLIENT_ID`，收件人写死为
`glycyrrhizin@outlook.com`，并自动带上 `state/mail.json` 的 SMTP 配置，然后分别调用：

```bash
python scripts/send_digest.py --plugin premier_league --calendar graph --to glycyrrhizin@outlook.com
python scripts/send_digest.py --plugin champions_league --calendar graph --to glycyrrhizin@outlook.com
```

日常不需要传参：

```bash
bash scripts/send_leagues.sh --dry-run     # 两个预览文件，不发信
bash scripts/send_leagues.sh               # 真发两封 + 写两个日历
```

临时换人仍可加 `--to someone@example.com` 覆盖。英超或欧冠休赛期没有比赛时，对应的那封会
自动跳过（`--allow-empty`），不报错。适合挂 cron，每周五 18:00 跑一次即可。

### `scripts/send_habits.py` 与 `scripts/habit_replies.py`

习惯打卡走“每日一封 + 邮件回复”的闭环。邮件主题固定为
`21天习惯养成 · 2026-11-14`；回信查询时用主题关键词 `21天习惯养成` 过滤。习惯邮件的
页头和发件人显示名使用 `21天习惯养成`，不会出现 `HPLINUX-Za7Za8`。

- `send_habits.py`：每天发一封「今日待办」。**同一天只发一封新邮件**，重复运行会跳过；
  发出去之后，当天的回复都在这一封里。
- `habit_replies.py`：从 IMAP 收件箱读回复，解析后写入 `state/habits.db`，再回一封
  「所有待办 + 本次变更」的摘要，保持在同一个邮件线程里。

会话粒度是**一天一串**：每封日报是新的主题、不带会话头，所以第二天自然另起一串；
回信从被回复的那封抄 `Thread-Index` / `Thread-Topic`（Outlook 的会话索引），同时带上
`In-Reply-To` / `References`（Gmail、QQ 认这两个）。少了 `Thread-Index`，Outlook 会把
“回复”拆成一封新邮件。

收件人默认写死为 `glycyrrhizin@outlook.com`；`send_habits.py` 负责“一天只发一封”，
直接跑 `send_digest.py --plugin habit` 不会做当天去重。数据库只存待办、打卡状态和已处理
的 `Message-ID`，**不保存邮件原文**。回信只处理发件人等于配置收件人的邮件。

```bash
python scripts/send_habits.py --dry-run     # 预览，不发送、不占当天名额
python scripts/send_habits.py               # 真发；今天发过就跳过
python scripts/habit_replies.py --dry-run   # 只解析最近回复，不写库不发信
python scripts/habit_replies.py             # 处理最近 3 天的回复
```

每日邮件示例（回复时只改数字/补原因，保留首字母）：

```text
1. 阅读 30 分钟
2. 运动 20 分钟
3. 早睡

P=完成  F=未完成（写原因）  A=新增  D=删除
P <编号...>
F <编号...> <原因>
A <每日待办，多个用分号>
D <编号...>
```

`P` = 完成，`F` = 未完成（必须写原因），`A` = 新增，`D` = 删除。`P/F/D`
后面可以接多个编号；`A` 新增多个待办时用分号分隔。格式有问题的行不会执行，回信摘要
会列出来并说明正确格式。

`未完成` 必须带原因，否则不会记录，并会在摘要里要求补原因；没提到的待办会继续显示为
「未回复」。新增的待办是**每天重复**的习惯；删除后不再出现在之后的邮件里。同一天多次
回复只更新同一份记录，`新增` 同一名称不会产生第二条待办。

过了当天还没回复的待办，下次发信或收信时会自动标成**未完成（当日未回复）**，不再
一直停在「未回复」——否则连续天数和完成率都会算错。判定边界是本地时间零点，只看
`day < 今天` 的记录；事后补回复仍然可以改成完成，迟到的「完成」照样算数。

每日邮件和回信摘要会为每个习惯显示统计：**自 2026-11-01 开始 · 共完成 12 次 · 连续 3 天**。

**IMAP 配置**：回复读的是 SMTP 发件箱的收件箱（QQ / 163 / Gmail 都可以）。QQ 邮箱需要
在设置里开启 IMAP，密码填授权码；`imap_host` 不写时会从 `smtp.qq.com` 推导成
`imap.qq.com`，端口默认 993。查询优先用 IMAP 服务端的 `SUBJECT "21天习惯养成"` 过滤，
服务器不支持时退回 `SINCE` + 本地头部过滤。数据库在 `state/habits.db`（已 gitignore），
可用 `MAILER_HABIT_DB` 改路径。

发件人过滤基于 `From` 头，**不验证 DKIM/SPF**；它用来防止误处理，不防伪造。个人自用
足够，但别把这个邮箱地址当成强认证。

适合挂 cron：

```cron
# 每天 07:00 发今日待办
0 7 * * * cd /home/pirate/work/Za7Za8 && python3 scripts/send_habits.py >> state/habit.log 2>&1

# 06:00-23:00 每 15 分钟收一次回复（单次约 1.4 秒；10 分钟也不会压垮服务器）
*/15 6-23 * * * cd /home/pirate/work/Za7Za8 && python3 scripts/habit_replies.py >> state/habit.log 2>&1
```

### `scripts/send_mail.py`

发一封**普通邮件**：任意标题 + 纯文本 / HTML + 附件。和摘要邮件分工不同：
`send_digest.py` 是“插件出内容、框架排版”，这个只负责“把东西发出去”。

```bash
python scripts/send_mail.py --to a@b.com                      # 用默认标题/正文
python scripts/send_mail.py --to a@b.com --subject 标题 --body "正文"
python scripts/send_mail.py --to a@b.com --subject 周报 --body-file note.txt
python scripts/send_mail.py --to a@b.com --subject 图文 --html page.html --attach 图.png
python scripts/send_mail.py --to a@b.com --dry-run            # 只写 .eml/.html
```

- 不写 `--subject` / `--body` 时用默认值：「发信测试」/「来自 send_mail.py」——
  这个脚本最常见的用途就是“先发一封测试信确认通道”。

- SMTP 配置和 `send_digest.py` 共用：环境变量或 `--config state/mail.json`；
  单独跑时普通邮件不套“每日摘要”那个显示名（`--brand` / `MAILER_BRAND` 可改）。
- `--dry-run` 把邮件写成 `.eml` + `.html` 放到 `downloads/mail_preview/`，
  能拖进 Outlook 先看一眼，不发信。
- 附件按扩展名猜 MIME，猜不到就用 `application/octet-stream`；`--body` 与
  `--body-file` 互斥，`-` 表示从标准输入读。
- **投递失败不会静默**：`smtplib` 把“部分收件人被拒”当返回值而不是异常，脚本会把它
  当失败抛出，并打印每个地址的服务端返回码；`--debug` 可看完整 SMTP 对话。
- Message-ID 用发件人的真域名（以前写死的 `@za7za8.local` 会被部分收件方扣分）。

### `scripts/graph_auth_check.py`

Graph 认证自检：设备代码流拿票 → 解码验签（纯标准库 RS256）→ 实调 `/me` 验证
这枚票到底能不能用。**取票 / 换票 / 缓存复用 `lib/graph_calendar.py`**，和
`send_digest.py --graph-login` 是同一套实现、同一个缓存文件，不会两边漂移。

```bash
python scripts/graph_auth_check.py --client-id <应用ID>                # 登录并检查
python scripts/graph_auth_check.py --client-id <应用ID> --probe-scopes # 同一枚 refresh_token 试几种 scope 写法
python scripts/graph_auth_check.py --reuse                            # 复用已有缓存，不重新登录
python scripts/graph_auth_check.py --token eyJ0eXAi...                # 只检查一枚现成的票
python scripts/graph_auth_check.py --proxy http://127.0.0.1:7890      # 走代理
```

判据只有一个：**Graph `/me` 返回 200**。个人账户（MSA）拿到的是 `EwB…` 开头的
不透明票，不是 JWT，但照样能用——别拿「是不是 JWT」当判据。
退出码：`0` = Graph 接受这枚票；`1` = 被拒；`2` = 参数 / 网络 / 授权问题。

### `scripts/graph_calendar_test.py`

写日历的自检：建测试日历 → 清场 → 写 3 条事件 → 验证幂等 → 故意造一条
副本验证清理 → 对比标题/时间 → 收尾清理。

```bash
python scripts/graph_calendar_test.py --dry-run   # 只读演练：不往日历写事件
python scripts/graph_calendar_test.py             # 真写一遍，跑完自动清理
python scripts/graph_calendar_test.py --keep      # 写完保留，方便去 Outlook 里看
```

只动独立的 `Automation/Za7Za8 测试` 日历，事件标题一律 `[测试]` 开头，清理也只删这些。
它能抓到“去重键不对导致重复建事件”这类问题——开发时就靠它发现 `sync_graph`
把标题当成时间戳截断的 bug。

## 文档

- [`docs/pikpak-share-to-direct-link.md`](docs/pikpak-share-to-direct-link.md)
  —— 从零开始讲「PikPak 分享链接 → 直链」的原理与实现，包含：

  - HTTP、SPA、预签名 URL、内容哈希、风控验证码、地理围栏等基础概念
  - 怎么自己从压缩过的前端 JS 里找出接口路径和签名算法
  - Step 1~9 逐步拆解，每步都是「原理 → 真实请求与响应 → 脚本代码」
  - 排查决策树、字段速查表、常见报错对照表
  - 每条结论都标注了是「实测」还是「读代码推断」

- [`docs/dbus-learning.md`](docs/dbus-learning.md)
  —— D-Bus 学习路线（Arch Linux / SSH 环境），按「命令行 → 概念 → 客户端 →
  服务端 → 读真实服务 → 进阶」拆成 6 步，包含：

  - Arch 特有的环境问题：dbus-broker 与 dbus-daemon 怎么选、SSH 下没有会话总线怎么办
  - 每一步都配了可直接执行的命令、代码和过关标准
  - 两周日程表、常见坑、自测清单，以及可逐条勾选的学习待办

- [`docs/outlook-graph-calendar.md`](docs/outlook-graph-calendar.md)
  —— 让 `send_digest.py` 把赛程写进 Outlook 日历，包含：

  - 为什么只有 Graph 这一条路（本机 COM 和 `.ics` 各自卡在哪）
  - Azure 应用注册的逐步操作，每步都写了「该看到什么」
  - 设备代码登录、`--graph-whoami` 验证、token 存哪、怎么换账号
  - 报错对照表（`AADSTS50020`、`700016`、`7000218`、`65001`）
  - cron 与换机器的注意事项，以及为啥这不是「绑定账户」

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
