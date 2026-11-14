# 用 Microsoft Graph 写 Outlook 日历

`scripts/send_digest.py` 抓完赛程后，会把下一轮的比赛写进你 Outlook 里一个专用日历。
写日历只有这一条路：直接调 Microsoft Graph。

为什么不用别的：

- **Windows 本机 Outlook（COM）** 只能在装了 Outlook 的桌面上跑，服务器上没有；
- **`.ics` 文件/附件** 得靠人手动导入，或者挂成邮件附件——两个都不想要。

Graph 是 HTTPS 接口，Windows、Linux 服务器、容器里都能调。

## 先分清两个账号

这里没有"绑定"这个动作。Graph 的 `/me` 永远指**授权登录的那个账号**，应用不绑任何账户。

| 账号 | 用在哪一步 | 决定什么 |
| --- | --- | --- |
| 随便哪个 Microsoft 账号 | 登录 [Azure 门户](https://portal.azure.com)、注册应用 | 只决定你能改哪个应用 |
| **要写入日历的那个账号** | `--graph-login` 时在浏览器里登录 | 决定日历写进谁家 |

所以"让应用访问 outlook 日历"的实际含义是：**注册一个允许个人账户的应用，再在登录那一步
用那个 outlook 账号同意**。门户用 QQ 邮箱注册的 Microsoft 账号登录完全没问题。

## 一、注册应用（约 10 分钟）

### 1. 打开门户

浏览器进 <https://portal.azure.com>，顶部搜索框输入 **应用注册**（*App registrations*）点进去。

### 2. 新注册

左上 **+ 新注册**（*New registration*），只填两项：

| 字段 | 填什么 |
| --- | --- |
| 名称 | 随便，比如 `pl-calendar` |
| 支持的账户类型 | **「任何组织目录中的账户和个人 Microsoft 账户」**（第三个单选） |

重定向 URI 留空——设备代码流不需要它。点 **注册**。

> **这一步选错就白搭。** 默认选项是「仅此组织目录中的账户」，个人 Outlook 账号会被挡在
> 门外，报「帐户在租户中不存在」。三个单选如果是灰的（老注册改不了），直接重建一个。

### 3. 抄客户端 ID

注册完跳到「概述」页，抄下 **应用程序(客户端) ID**（*Application (client) ID*），
形如 `00000000-0000-0000-0000-000000000000`。

**别抄错**：不是「对象 ID」，也不是「目录(租户) ID」。

### 4. 打开公共客户端流

左侧 **身份验证**（*Authentication*）→ 拉到最下面 **高级设置** →
**允许公共客户端流**（*Allow public client flows*）选 **是** → 顶部 **保存**。

设备代码流必须要这一项，否则登录进行到一半会报 `AADSTS7000218`。

### 5. 加日历权限

左侧 **API 权限** → **+ 添加权限** → **Microsoft Graph** → **委托的权限**
（*Delegated permissions*）→ 搜索 `Calendars` → 勾 **`Calendars.ReadWrite`** →
**添加权限**。

个人账户不需要「代表组织授予管理员同意」，同意动作在登录那一步由你自己完成。

## 二、在机器上登录拿 token

在**要跑定时任务的那台机器**上执行（token 要落在它本地）：

```bash
export MAILER_GRAPH_CLIENT_ID=<第 3 步抄的客户端 ID>
python scripts/send_digest.py --graph-login
```

终端会给出验证地址（通常已带上代码）和有效期：

```
浏览器打开这个链接（码已带上）：
  https://microsoft.com/devicelogin?otc=ABCD1234
用要写入日历的那个邮箱登录并同意，900 秒内有效
```

浏览器打开它，用要写日历的邮箱登录并同意；链接没带代码时，手输终端里给的 8 位码。

- 登录页**不要用默认账号**，点「使用其他帐户」→ 输入要写日历的邮箱；
- 权限确认页看到「读取和写入你的日历」→ **接受**。

几秒后终端出现：

```
登录成功，token 已存到 `<仓库>/state/.graph_token.json`
```

卡在等待不动，通常是浏览器那步没走完（登错号、或者没点「接受」）。900 秒会超时，重跑就行。

## 三、验证

```bash
python scripts/send_digest.py --graph-whoami
```

```
当前 token 属于：你的昵称
账号：you@outlook.com
日历会写到这个账号的邮箱里
```

显示的不是目标账号，就 `--graph-logout` 后重来第二步。

再跑一次预览，顺便真写日历：

```bash
python scripts/send_digest.py --dry-run --apply-calendar
```

正文底部出现 `日历：新建 10 场、更新 0 场（「英超赛程 2026-27」）` 就成了，
打开 <https://outlook.live.com/calendar> 能看到这个日历文件夹。

## 四、报错对照

| 报错 | 原因 | 回哪一步 |
| --- | --- | --- |
| `AADSTS50020`、「帐户在租户中不存在」 | 账户类型不是「组织 + 个人」 | 一 · 2 |
| `AADSTS50019`、「租户中不存在该用户」 | 用了租户专属端点登个人账号 | 去掉 `MAILER_GRAPH_TENANT`，用 `common` |
| `AADSTS700016`、「应用不存在」 | client ID 抄错，或抄成了对象 ID | 一 · 3 |
| `AADSTS7000218`、「需要 client_assertion」 | 没开「允许公共客户端流」 | 一 · 4 |
| `AADSTS65001`、「用户或管理员未同意」 | 权限没加，或浏览器里点了拒绝 | 一 · 5 / 二 |
| `AADSTS90002`、「租户不存在」 | `MAILER_GRAPH_TENANT` 填的不是租户 ID | 去掉 `MAILER_GRAPH_TENANT`，用默认 `common` |
| 终端一直等，没有结果 | 浏览器那步没完成 | 二 |
| `--graph-whoami` 显示别的账号 | 登录时登错号 | `--graph-logout` 后重来 |

## 五、个人账户的 access_token 不是 JWT

个人账户（`@outlook.com`）拿到的 access_token 常常是 `EwB…` 开头的**不透明长串**，
一个点都没有——这是正常的，Graph 照收。判断一枚票能不能用，**唯一的标准是拿它调一次
`GET /me`**，返回 200 就是能用；别按「是不是 JWT」拦截，那会把个人账户的票全部误杀。

`--graph-whoami` 做的就是这件事；登录时如果提示「不透明票」，可以不管。

## 六、token 存在哪

`<仓库>/state/.graph_token.json`，可以用 `MAILER_GRAPH_TOKEN` 改路径。内容是：

```json
{"client_id": "...", "tenant": "common", "refresh_token": "...", "access_token": "...", "expires_at": 1762...}
```

- **等于密码**：`state/` 已经在 `.gitignore` 里，别改到别的目录、别贴出来；
- 过期会自动用 `refresh_token` 续，不用管；
- 换账号、换应用、被撤销授权：`python scripts/send_digest.py --graph-logout`，然后重新登录；
- 在 QQ 邮箱的「已授权应用」里也能撤销，撤销后 token 立即失效。

## 七、定时任务与换机器

**cron 必须和登录时的系统用户一致**，否则读不到 `state/.graph_token.json`。用 root 的
crontab 就跑在 root 下、文件也放在 root 能读到的地方。

换机器有两种做法：

1. 在新机器上重新 `--graph-login`（推荐，干净）；
2. 把 `.graph_token.json` 拷过去、`chmod 600`（省一次登录，但等于搬运密码）。

## 八、企业/学校账号

组织账号的权限可能需要管理员同意，这时 API 权限页会出现「代表 <租户> 授予管理员同意」按钮。
个人账户不会出现，也不需要。

如果要写的是**别人**的日历（共享邮箱、会议室），`/me` 就不够了，得用 `/users/{id}/events`，
并且需要额外的应用权限——那属于另一种部署，这个脚本没做。
