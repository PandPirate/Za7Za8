# 学 D-Bus：从命令行到服务端（Arch Linux / SSH 环境）

> 这是一份**学习路线**，不是 API 手册。目标是让你在两周内从「没听过 D-Bus」到
> 「能自己写一个导出对象、发信号、能被别人调用的服务」。
>
> 适用环境：Arch Linux，基本通过 SSH 访问（无桌面、无本地终端）。
> 文中命令按顺序可直接执行；涉及具体版本差异的地方标了「需确认」，以你机器上的
> 实际输出为准。相关背景概念见 [`../README.md`](../README.md)。

## 目录

- [0. 学习顺序（先看这个）](#0-学习顺序先看这个)
- [1. 环境准备](#1-环境准备)
- [2. 第 1 步：用命令行当「用户」（1–2 天）](#2-第-1-步用命令行当用户12-天)
- [3. 第 2 步：建立概念模型（半天）](#3-第-2-步建立概念模型半天)
- [4. 第 3 步：写客户端（1 天）](#4-第-3-步写客户端1-天)
- [5. 第 4 步：写服务端（1–2 天）](#5-第-4-步写服务端12-天)
- [6. 第 5 步：读真实服务（2 天）](#6-第-5-步读真实服务2-天)
- [7. 第 6 步：进阶（按需）](#7-第-6-步进阶按需)
- [8. 两周日程表](#8-两周日程表)
- [9. 常见坑](#9-常见坑)
- [10. 自测清单](#10-自测清单)
- [11. 资源](#11-资源)
- [12. 学习待办](#12-学习待办)

---

## 0. 学习顺序（先看这个）

D-Bus 的规范有 200 多页，90% 是你写业务代码时永远用不到的线格式和 SASL 认证细节。
**不要从规范读起**，按这个顺序走：

1. **先当用户**：用 `busctl` / `gdbus` 在总线上摸来摸去，不写一行代码。
2. **再当客户端**：调别人暴露的方法、订阅别人的信号。
3. **最后当服务端**：自己申请总线名、导出对象、发信号。
4. **规范当字典**：只在需要时查「Type system」和「Message bus」两章。

一条链记住所有概念：

```
地址（总线） → 总线名 → 对象路径 → 接口 → 成员（方法/信号/属性）
```

任何一次 D-Bus 交互，都是在这条链上定位到唯一一个成员。

---

## 1. 环境准备

### 1.1 确认已经装了什么

Arch 上 `dbus` 是 `systemd` 的依赖，通常不用手动装：

```bash
pacman -Q dbus systemd glib2
busctl --version
gdbus --version
```

缺 `gdbus` 就装 `glib2`：

```bash
sudo pacman -S --needed glib2 dbus
```

### 1.2 先搞清楚你的总线是哪个实现在跑

Arch **默认使用 dbus-broker**（不是参考实现 dbus-daemon），两者只能装一个：

```bash
pacman -Q dbus-broker-units dbus-daemon-units 2>/dev/null
systemctl status dbus --no-pager
```

- 输出 `dbus-broker-units` → 你跑的是 dbus-broker；
- 输出 `dbus-daemon-units` → 参考实现。

为什么先查这个：

- 出问题时日志看哪个 unit、配置支持到什么程度，都和实现有关；
- 两个实现对策略文件的兼容度不完全一样，遇到 `AccessDenied` 时先确认实现再查文档；
- dbus-broker 目前不支持 AppArmor 对 D-Bus 的 mediation（官方内核也不支持），
  如果你的安全方案依赖 AppArmor，这点要提前知道。

> 想换实现：`sudo pacman -S dbus-daemon-units`（会自动替换 `dbus-broker-units`）。
> 学习阶段没必要换，跟着默认走就行。

### 1.3 SSH 下的两条总线

| 总线 | 谁说 | 地址 | 在 SSH 里 |
|---|---|---|---|
| system | 系统服务（systemd、logind、NetworkManager…） | `/run/dbus/system_bus_socket` | **一定有** |
| session | 当前登录用户的桌面应用 | 通常是 `$XDG_RUNTIME_DIR/bus` | **取决于 user manager 是否在跑** |

先查：

```bash
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"
busctl --user list
```

- `busctl --user list` 有输出 → 会话总线可用，直接用。
- 报 `Failed to connect to user scope bus ... No such file or directory` → 见下面三种办法。

**办法 A：让 user manager 常驻**（推荐，SSH 重复登录时省事）

```bash
sudo loginctl enable-linger "$USER"
```

开了 linger 后，即使用户没登录，`systemd --user` 也在跑，`$XDG_RUNTIME_DIR/bus` 一直存在。

**办法 B：临时开一条私有会话总线**（最干净，练习时首选）

```bash
dbus-run-session -- bash
```

这条命令会启动一个临时的 `dbus-daemon`，把地址写进 `DBUS_SESSION_BUS_ADDRESS`，
然后开一个子 shell。这个 shell 里所有 `--session` / `--user` 命令都能跑，
退出后总线随之销毁——**不会污染你的真实会话**，非常适合做实验。

**办法 C：手动连一条已有总线**

```bash
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus busctl --user list
```

> 注意：`sudo` 之后环境变量会被清掉，`sudo busctl --user` 大概率连不上用户总线。
> 要提权就用 `sudo busctl --system`，别混着用。

### 1.4 工具从哪来

| 命令 | 包 | 用途 |
|---|---|---|
| `busctl`、`systemctl`、`journalctl` | `systemd` | 浏览、调用、监控总线；看日志 |
| `gdbus`、`gdbus-codegen` | `glib2` | 另一套命令行工具；从 XML 生成 C 代码 |
| `dbus-send`、`dbus-monitor`、`dbus-run-session` | `dbus` | 经典工具，脚本里常见 |
| `python-dbus`、`python-gobject` | `python-dbus`、`python-gobject` | Python 绑定（本文示例用） |
| `d-spy` | `d-spy` | 图形化浏览总线（需要 X，SSH 下得 `ssh -X`） |

安装：

```bash
sudo pacman -S --needed dbus glib2 python-dbus python-gobject
```

### 1.5 SSH 下的工作姿势

开一个 `tmux`，分两个窗格：**一个常驻 `busctl monitor`，一个敲命令**。
这样每敲一条命令，都能立刻看到它在总线上产生了什么消息——这是学 D-Bus 最快的方式。

```bash
tmux new -s dbus
# Ctrl-b %  左右分屏
```

---

## 2. 第 1 步：用命令行当「用户」（1–2 天）

**这一节不要写代码。** 目标是建立「总线上有什么、消息长什么样」的直觉。

### 2.1 浏览

```bash
# 系统总线上有哪些服务（不加 --user 默认就是 system）
busctl list

# 某个服务的对象树
busctl tree org.freedesktop.systemd1

# 某个对象的接口、方法、信号、属性
busctl introspect org.freedesktop.systemd1 /org/freedesktop/systemd1

# gdbus 的等价写法（输出更易读）
gdbus introspect --system --dest org.freedesktop.systemd1 \
    --object-path /org/freedesktop/systemd1
```

### 2.2 调用

```bash
# busctl：服务 / 对象 / 接口 / 方法 / 签名 / 参数
busctl call org.freedesktop.DBus /org/freedesktop/DBus \
    org.freedesktop.DBus GetNameOwner s org.freedesktop.systemd1

# gdbus
gdbus call --system --dest org.freedesktop.DBus \
    --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.ListNames

# dbus-send（老工具，很多脚本里还在用）
dbus-send --system --print-reply --dest=org.freedesktop.DBus \
    /org/freedesktop/DBus org.freedesktop.DBus.ListNames
```

三个工具做的是同一件事，区别只在参数格式。**建议主力用 `busctl`**：它能显示
方法的完整签名，参数类型不容易猜错。

### 2.3 监控（最关键的一步）

```bash
# 终端 A：盯着系统总线
busctl --system monitor

# 终端 B：制造一点动静
sudo systemctl restart sshd
```

你会看到 `UnitNew`、`JobNew`、`PropertiesChanged` 之类的信号成串出现。
**这就是 D-Bus 的日常**：方法调用是点对点，信号是广播。

### 2.4 三个必做实验

1. **看一次往返**：一边 `busctl --system monitor`，一边执行
   `busctl call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus ListNames`。
   观察一对 `method call` / `method return`，注意 `serial` 和 `reply_serial` 是配对的。
2. **看名字归属**：`busctl call ... GetNameOwner s org.freedesktop.systemd1`，
   返回一个 `:1.xx` 形式的 unique name。再 `busctl list | grep systemd` 对照。
3. **看属性**：
   `busctl get-property org.freedesktop.systemd1 /org/freedesktop/systemd1/unit/sshd_2eservice org.freedesktop.systemd1.Unit ActiveState`
   （对象路径里的 `.` 被转义成了 `_2e`）。

### 2.5 过关标准

不看资料，能说出：一次方法调用要指定哪 5 样东西，哪些可以省略，为什么。

---

## 3. 第 2 步：建立概念模型（半天）

读 Havoc Pennington 那篇经典教程（不长，讲概念而不是 API）：
[D-Bus Tutorial](https://dbus.freedesktop.org/doc/dbus-tutorial.html)。

### 3.1 六个概念

| 概念 | 类比 | 例子 |
|---|---|---|
| 总线（system / session） | 两个独立的「局域网」 | system 管系统服务，session 管当前用户 |
| 总线名（bus name） | 域名 / IP | `org.freedesktop.systemd1`（well-known）/ `:1.42`（unique） |
| 对象路径（object path） | 文件路径 | `/org/freedesktop/systemd1/unit/sshd_2eservice` |
| 接口（interface） | 一组成员的命名空间 | `org.freedesktop.DBus.Properties` |
| 成员（member） | 方法 / 信号 / 属性 | `Get` / `PropertiesChanged` / `ActiveState` |
| 签名（signature） | 参数类型串 | `a{sv}`、`(ii)`、`as` |

两类名字的区别要刻进脑子：

- **unique name**（`:1.42`）：连接建立时由总线分配，**永不重复**，进程退出即失效。
- **well-known name**（`org.freedesktop.systemd1`）：需要主动申请，**可以被接管**，
  进程重启后还是同一个名字。

这就是为什么「按 well-known name 订阅信号」在对方重启后会失效——名字换了主人。

### 3.2 类型签名速查

```
y byte    b bool     n/q int16/uint16    i/u int32/uint32
x/t int64/uint64     d double           s string
o object path        g signature        v variant        h fd
a 数组    () 结构体   {} 字典
```

例：`a{sv}` = 字典，键是字符串，值是 variant；`(is)` = 一个 int + 一个 string 的结构体。

### 3.3 四个标准接口（协议自带的公共设施）

| 接口 | 作用 |
|---|---|
| `org.freedesktop.DBus.Introspectable` | `Introspect()` 返回 XML，描述对象有哪些接口 |
| `org.freedesktop.DBus.Properties` | `Get` / `GetAll` / `Set` + `PropertiesChanged` 信号 |
| `org.freedesktop.DBus.ObjectManager` | `GetManagedObjects()` + 增删信号（动态对象树的标准做法） |
| `org.freedesktop.DBus.Peer` | `Ping` / `GetMachineId` |

`busctl introspect` 之所以能工作，就是因为它调了 `Introspectable.Introspect`。

### 3.4 规范怎么读

[D-Bus Specification](https://dbus.freedesktop.org/doc/dbus-specification.html) 只读三块：

1. **Type system**：签名语法，写代码天天用；
2. **Message bus**：名字、match rule、服务激活、内省、标准接口；
3. 线格式（marshalling / SASL）——**先跳过**，等要抓包或自己实现绑定再看。

---

## 4. 第 3 步：写客户端（1 天）

### 4.1 准备

```bash
sudo pacman -S --needed python-dbus python-gobject
```

### 4.2 调方法

```python
#!/usr/bin/env python3
import dbus

bus = dbus.SystemBus()
proxy = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
iface = dbus.Interface(proxy, "org.freedesktop.DBus")

names = iface.ListNames()
print(len(names), "个名字")
print(iface.GetNameOwner("org.freedesktop.systemd1"))
```

> `dbus-python` 会先做内省，自动把 Python 值转成签名对应的类型；内省失败时
> 必须手动指定 `dbus.String` / `dbus.UInt32` 这类包装类型，否则报 `TypeError`。

### 4.3 订阅信号（这里必须用主循环）

```python
#!/usr/bin/env python3
import dbus, dbus.mainloop.glib
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()

def on_unit_new(name, primary):
    print("新 unit:", name, primary)

bus.add_signal_receiver(on_unit_new,
                        signal_name="UnitNew",
                        dbus_interface="org.freedesktop.systemd1.Manager",
                        bus_name="org.freedesktop.systemd1")

GLib.MainLoop().run()
```

跑起来后另开一个终端 `sudo systemctl restart sshd`，就能看到输出。

### 4.4 过关标准

能写一个脚本，订阅某个真实服务的信号并打印出来，进程挂后台不退出。

---

## 5. 第 4 步：写服务端（1–2 天）

从「调别人」变成「被别人调」。先**在会话总线上练**（不需要任何权限配置），
等跑通了再考虑 system bus（那需要策略文件，见第 7 节）。

### 5.1 一个完整的服务

```python
#!/usr/bin/env python3
import dbus, dbus.service, dbus.mainloop.glib
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SessionBus()

# 必须保留引用，否则名字会被释放
name = dbus.service.BusName("org.example.Hello", bus)

class Hello(dbus.service.Object):
    def __init__(self):
        super().__init__(bus, "/org/example/Hello")

    @dbus.service.method("org.example.Hello", in_signature="s", out_signature="s")
    def Echo(self, text):
        return f"hello, {text}"

    @dbus.service.method("org.example.Hello", in_signature="", out_signature="")
    def Trigger(self):
        self.Pinged("from Trigger")

    @dbus.service.signal("org.example.Hello", signature="s")
    def Pinged(self, text):
        pass

Hello()
GLib.MainLoop().run()
```

### 5.2 用命令行验证（不要先写客户端）

```bash
# 在 dbus-run-session 的 shell 里跑服务，另开一个同样的 shell 验证
busctl --user introspect org.example.Hello /org/example/Hello
busctl --user call org.example.Hello /org/example/Hello org.example.Hello Echo s world
busctl --user call org.example.Hello /org/example/Hello org.example.Hello Trigger
busctl --user monitor        # 看 Pinged 信号
```

也可以只对某个接口过滤信号：

```bash
dbus-monitor --session "type='signal',interface='org.example.Hello'"
```

### 5.3 理解内省 XML

`busctl introspect` 的输出背后是一段 XML，长这样：

```xml
<node>
  <interface name="org.example.Hello">
    <method name="Echo">
      <arg type="s" name="text"  direction="in"/>
      <arg type="s" name="reply" direction="out"/>
    </method>
    <signal name="Pinged">
      <arg type="s" name="text"/>
    </signal>
  </interface>
</node>
```

**重要习惯**：真实项目里应该先写这份 XML 当接口定义（ABI），再用
`gdbus-codegen` / `sdbus-c++-xml2cpp` 生成骨架代码。手写方法分发容易漏掉
`Properties` 接口和 `PropertiesChanged` 信号，这是最常见的 bug 来源。

### 5.4 过关标准

你的服务能被 `busctl`、`gdbus` 和你自己的客户端同时调用，`introspect` 返回的
方法和你在代码里定义的一致。

---

## 6. 第 5 步：读真实服务（2 天）

无桌面的 Arch 上，最好的靶子就是 systemd 自己：

```bash
busctl tree org.freedesktop.systemd1 | head -30
busctl introspect org.freedesktop.systemd1 /org/freedesktop/systemd1/unit/sshd_2eservice
```

推荐按这个顺序读：

| 服务 | 看点 |
|---|---|
| `org.freedesktop.systemd1` | 最复杂的真实案例：Manager / Unit / Job 三类对象，事务语义 |
| `org.freedesktop.login1` | 会话、电源管理，`PrepareForShutdown` 这类信号 |
| `org.freedesktop.hostname1` | 最小完整案例：一两个属性 + 几个方法 |
| `org.freedesktop.timedate1` | 同上，适合对照着看 |
| `org.freedesktop.resolve1` | 动态对象 + 配置方法（需启用 systemd-resolved） |

读的时候只问三个问题：

1. **对象怎么切？** 为什么是 `/unit/xxx` 而不是一个大对象？
2. **属性怎么分组？** 哪些是状态、哪些是配置？
3. **哪些变化用信号、哪些用属性？** 状态同步用 `PropertiesChanged`，
   一次性事件用信号。

> 想动手的话：`busctl call org.freedesktop.hostname1 /org/freedesktop/hostname1 org.freedesktop.hostname1 SetHostname s testhost`
> —— 会真的改主机名，记得改回来。

---

## 7. 第 6 步：进阶（按需）

到这一步你已经会用了，剩下的按场景挑：

- **服务激活**：`.service` 文件放在 `/usr/share/dbus-1/services/`（会话总线）
  或 `/usr/share/dbus-1/system-services/`（系统总线），第一次被调用时按需拉起。
  systemd 托管的服务用 `dbus-*.service` + `BusName=`。
- **安全策略**：系统总线默认「能收、不能发 method call、不能 own 名字」，
  需要服务自己的 `<policy>` 文件开洞。Arch 上包提供的策略在
  `/usr/share/dbus-1/system.d/`，本地覆盖放 `/etc/dbus-1/system.d/`
  （以 `/usr/share/dbus-1/system.conf` 里的 `includedir` 为准）。
- **C 语言路线**：GDBus（[GIO 文档](https://docs.gtk.org/gio/)）或 sd-bus。
- **抓包**：`busctl capture --system > bus.pcapng`，把文件拷回本地用 Wireshark 打开
  ——这条对 SSH 环境特别有用，因为 `d-spy` 需要图形界面。
- **协议细节**：marshalling、SASL 认证、fd passing、p2p 直连。

---

## 8. 两周日程表

| 天 | 做什么 | 产出 |
|---|---|---|
| 1 | 环境检查、`busctl` 浏览、`monitor` 看消息 | 能说清两条总线的区别 |
| 2 | 三个实验 + 读教程 + 规范三章 | 概念模型笔记 |
| 3–4 | 写客户端：调方法、订阅信号 | 一个能常驻的信号监听脚本 |
| 5–6 | 写服务端：导出对象、发信号 | 能被 `busctl` 调通的 Hello 服务 |
| 7 | 写内省 XML + 用 codegen 生成骨架 | 接口定义文件 |
| 8–9 | 读 systemd 的 3 个服务接口 | 一份对象模型分析 |
| 10 | 服务激活 + 策略文件（系统总线） | 一个能在 system bus 上跑的服务 |
| 11–12 | 抓包、异常处理、超时、重连 | 更接近生产代码的版本 |
| 13–14 | 复习 + 自测清单 + 写自己的小工具 | 一个解决实际问题的 D-Bus 工具 |

---

## 9. 常见坑

1. **system 和 session 搞混**。`busctl` 不加参数默认走 system；桌面教程里的
   `--session` 在 SSH 里经常连不上。
2. **`sudo` 丢环境变量**。`sudo busctl --user` 基本必然失败，提权就用 `--system`。
3. **同步调用卡死主循环**。服务在自己的主循环里同步调用自己，或者客户端在
   回调里做阻塞操作，都会死锁。异步 API 不是可选项。
4. **默认 25 秒超时**。长任务要么异步，要么返回一个 job 对象再等信号。
5. **属性不发 `PropertiesChanged`**。客户端缓存永远不更新，这类 bug 极难查。
6. **well-known name 重启后失效**。监听 `NameOwnerChanged`，或者用高层库的自动重连。
7. **签名对不上**。`v` 套 `a{sv}` 是最容易写错的地方，先用 `busctl` 验证签名再写代码。
8. **监控看不到东西**。Arch 默认的 dbus-broker 对 monitor 的权限处理和
   dbus-daemon 不完全一样，`dbus-monitor` 报 `AccessDenied` 时换 `busctl monitor`，
   或者直接看 `journalctl`。

---

## 10. 自测清单

能答上来就算入门：

1. 一次方法调用要指定哪 5 个要素？哪些可以省略？
2. `:1.42` 和 `org.freedesktop.systemd1` 有什么区别？谁能被接管？
3. 信号和方法调用的根本区别是什么？为什么信号没有返回值？
4. `org.freedesktop.DBus.Properties` 解决了什么问题？为什么不直接用普通方法暴露属性？
5. 你的服务没在跑，但配置了服务激活，第一次调用会发生什么？
6. 为什么推荐订阅 `PropertiesChanged` 而不是轮询 `Get`？

---

## 11. 资源

### 必读（按顺序）

1. [D-Bus Tutorial](https://dbus.freedesktop.org/doc/dbus-tutorial.html) — 概念，半天
2. [dbus-python tutorial](https://dbus.freedesktop.org/doc/dbus-python/tutorial.html) — 最快上手的代码路径
3. [D-Bus Specification](https://dbus.freedesktop.org/doc/dbus-specification.html) — 当字典查
4. [ArchWiki: D-Bus](https://wiki.archlinux.org/title/D-Bus) — Arch 特有的实现选择和调试工具
5. [ArchWiki 中文版：D-Bus](https://wiki.archlinuxcn.org/wiki/D-Bus) — 同上，中文

### 工具与手册

- [busctl(1)](https://www.freedesktop.org/software/systemd/man/latest/busctl.html) — 你 80% 的时间会花在这上面
- `man dbus-daemon`、`/usr/share/dbus-1/system.conf` — 总线的配置长什么样
- [d-spy](https://gitlab.gnome.org/GNOME/d-spy) — 图形化浏览总线（SSH 下需 X 转发）
- [GIO / GDBus API](https://docs.gtk.org/gio/) — C 语言路线的权威参考

### 想学得更深

- [zbus book](https://z-galaxy.github.io/zbus/) — 目前写得最好的「概念 + 代码」教材，不写 Rust 也值得读
- [Using sdbus-c++](https://kistler-group.github.io/sdbus-cpp/docs/using-sdbus-c++.html) — C++ 路线
- [OpenCloudOS《dbus 机制和使用》](https://docs.opencloudos.org/OCS/AdministratorGuide/System_Service_Management/Dbus_guide/) — 中文，覆盖面广

---

## 12. 学习待办

按顺序勾，勾完就是「能用 D-Bus 干活」的水平。

### 环境

- [ ] `pacman -Q dbus systemd glib2` 确认依赖齐全
- [ ] 确认总线实现（dbus-broker 还是 dbus-daemon）
- [ ] 用 `busctl --user list` 确认会话总线可用（不可用就 `dbus-run-session -- bash`）
- [ ] `tmux` 分屏：一边 `busctl monitor`，一边敲命令

### 阶段一：命令行

- [ ] `busctl list` / `tree` / `introspect` 各跑通一次
- [ ] 用 `busctl`、`gdbus`、`dbus-send` 三种方式调同一个方法
- [ ] 观察一对 method call / method return，说清 serial 的作用
- [ ] `GetNameOwner` 查一个 well-known name 的 unique name
- [ ] 读 `/usr/share/dbus-1/system.conf`

### 阶段二：概念

- [ ] 读完 D-Bus Tutorial
- [ ] 背下 6 个概念和签名速查表
- [ ] 读规范「Type system」「Message bus」两章
- [ ] 说清四个标准接口各自解决什么问题

### 阶段三：客户端

- [ ] Python 调通 `ListNames`
- [ ] 订阅 `UnitNew` 或 `PropertiesChanged` 并打印
- [ ] 处理一次「服务不存在」的报错

### 阶段四：服务端

- [ ] 会话总线上导出 `/org/example/Hello`
- [ ] 用 `busctl call` 调通自定义方法
- [ ] 发一个信号，并在 `dbus-monitor` 里看到
- [ ] 加一个属性，确认 `PropertiesChanged` 被触发
- [ ] 写一份内省 XML，用 codegen 生成代码

### 阶段五：真实服务与进阶

- [ ] 读懂 systemd 的 Manager / Unit 对象模型
- [ ] 在系统总线上 own 一个名字（写一个 policy 文件）
- [ ] 用 `busctl capture` 抓包并在 Wireshark 里打开
- [ ] 给自己的服务加异常处理和超时
