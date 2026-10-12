# 大司农（DSN）

局域网（含手机热点）下自动发现设备，**互传文件 / 图片 / 文件夹**，并支持**聊天式互发信息**。

**无服务器、不走互联网**：所有数据只在同一局域网内点对点传输。**加密始终开启**（每设备独立身份），
首次与某台设备连接时需要**配对确认**（两端核对同一个 6 位数字）。

> 从零自己实现的项目，定位与 [LocalSend](https://github.com/localsend/localsend) 同类（**不是它的 fork**）。
> 额外提供：聊天式收发文本、**并行分块 + 断点续传**、多设备同时收发、
> Android 端**分享已安装应用**、Windows 端**拖拽即发送**、传输记录持久化与清理、
> **每设备身份 + 配对**（可区分设备、可解绑）。

支持的组合：Android ↔ Windows、Android ↔ Android、Windows ↔ Windows。

---

## 特性

**传输**
- 文件 / 图片 / 文件夹（保留目录树）互传，**并行分块** + **断点续传**（断线后接着传）
- 多台设备**同时**收发，互不干扰
- 同名文件不互相覆盖（原子占位 + 改名）；传输记录**持久化**，可单条删除或一键清空

**发现与连接**
- UDP 广播自动发现（按 `device_id` 去重、超时剔除），开箱即用
- **手机热点场景**：一台开热点、另一台连上即可互通（Android 不向应用暴露热点网卡，走 TCP 直连握手兜底）
- 支持**手动添加对方 IP**；记住历史设备，重启后自动重连
- **协议版本闸门**：发现到版本不一致的设备时只显示、不拨号，并提示"需升级"

**信息**
- 聊天式收发文本，含「待发送 → 已发送 → 已送达」状态
- 会话可新建 / 删除；可**一键清空聊天记录**，也可**单条删除**

**桌面端（Windows）**
- 四页界面：附近 / 会话 / 传输 / 设置；托盘驻留 + 通知
- **把文件或文件夹直接拖进窗口即发送**（拖到某台设备上就发给它）
- 传输列表双击打开文件；删除时连同已落盘文件一起清理

**移动端（Android）**
- Compose 界面；通知 + 前台服务后台驻留（可选开机自启）
- **分享已安装应用**：把手机上装好的 App 以 APK 发给对方（分包应用自动打包成 `.apks`）
- 收到 `.apk` 点一下即可调起系统安装器

**安全（强制）**
- **每设备独立身份**（首次运行随机生成 P-256 长期密钥，私钥经系统加密保存）
- 每条 TCP 连接套标准 TLS；身份由**应用层双向 ECDSA 签名**证明（随机数防重放、对端指纹防中继）
- **首次连接需配对**：两端显示**同一个 6 位数字**，人工核对一致后各点一次「配对」；
  之后长期信任，可在设置里「解绑」
- **未配对的设备发不了任何业务数据**；界面只显示那 6 位数字，**不显示本机指纹**

---

## 目录结构

```
DSN-android-windows/
├── protocol/PROTOCOL.md        # 协议唯一事实源（当前 PROTO_VERSION = 4）
├── scripts/                    # 一键打包脚本（PowerShell + .bat 包装）
├── 软件图标/大司农.jpg          # 应用图标（打包时自动转 .ico / 自适应图标）
├── 大司农-android/             # Android 端（Kotlin）
│   ├── UI/                     #   :ui       Compose 前端    (com.dasinong.ui)
│   ├── 后端/                    #   :backend  网络 / 协议 / 传输 (com.dasinong.backend)
│   └── gradle/libs.versions.toml
└── 大司农-win/                 # Windows 端（Python）
    ├── 后端/dasinong_core/     #   网络 / 协议 / 传输（纯 stdlib + psutil）
    ├── UI/dasinong_ui/         #   PySide6 前端
    ├── run.py                  #   入口
    ├── smoke_test.py           #   单机双实例端到端自测
    └── tests/                  #   单元测试
```

---

## 快速开始

### Windows 端

```bash
cd 大司农-win
pip install -r requirements.txt

python run.py                 # 启动 GUI（关闭窗口 = 最小化到托盘，仍在后台收文件）
python smoke_test.py          # 端到端自测：发现 / 配对 / 文本 / 文件 / 文件夹 / 断点续传 / 解绑
python -m pytest tests -q     # 单元测试
```

单机跑两个实例联调（互不干扰）：

```bash
python run.py
DASINONG_TCP_PORT=37022 DASINONG_DATA_DIR=./_data_b python run.py
```

可用环境变量：`DASINONG_UDP_PORT` / `DASINONG_TCP_PORT` / `DASINONG_DATA_DIR` /
`DASINONG_RECEIVE_DIR` / `DASINONG_DEVICE_NAME` / `DASINONG_PREFS`。

> `DASINONG_PAIR_AUTO=1` 是**自测用**的开关（自动接受配对，无人值守时用）；正常使用**不要**设置。

### Android 端

用 **Android Studio** 打开 `大司农-android/` 目录（`File → Open`），Gradle Sync 后直接 Run。

| 项 | 版本 |
|---|---|
| Gradle | 9.8.0 |
| AGP | 9.4.1 |
| Kotlin | 2.4.20（AGP 9 **内置 Kotlin**，模块里**不要**再套 `org.jetbrains.kotlin.android`） |
| JDK | 17+（21 已验证） |
| compileSdk / targetSdk | 37 |
| minSdk | 26 |
| Compose BOM | 2026.09.00 |

命令行构建（可选）：

```bash
cd 大司农-android
gradle :backend:testDebugUnitTest :ui:assembleDebug
# 产物：UI/build/outputs/apk/debug/*.apk
```

> 仓库里**只有** `gradle/wrapper/gradle-wrapper.properties`，没有 `gradlew` / `gradle-wrapper.jar`。
> Android Studio 能直接用它定位 Gradle 发行版；若想用命令行 `./gradlew`，先执行一次
> `gradle wrapper --gradle-version 9.8.0` 生成脚本与 jar。

**注意**：AGP 对**非 ASCII 路径**敏感，`gradle.properties` 里保留了
`android.overridePathCheck=true`（并抑制了相关同步告警）。**不要删掉**，除非把工程挪到纯 ASCII 路径。

---

## 使用说明

**配对（首次连接）**：与某台设备第一次连接时，两端会各自弹窗显示**同一个 6 位数字**。
**核对两边数字完全相同**后，各点一次「配对」；两端都同意才会建立长期信任。
（数字不一样说明有人在中间冒充，请点「取消」。）之后不再需要确认，直到你在设置里「解绑」。

**附近页**：列出同一局域网内发现的设备，显示「已连接 / 未连接」以及**已配对 / 未配对 / 版本过旧**。
可对某台设备直接：打开会话、发文件、发图片、发文件夹、**分享应用**。

**会话页**：聊天式收发文本。输入框右侧圆形按钮发送；下方可发图片 / 文件 / 应用。
标题栏「清空」可一键清空本会话记录；长按（手机）/ 右键（电脑）气泡可删除单条。

**传输页**：所有收发任务与进度。可打开文件、单条删除、一键清空（会连同已落盘文件一起清理；
**进行中的任务会保留**）。

**Windows 拖拽**：把文件或文件夹**直接拖进窗口**即发送。目标按此顺序确定：
拖到某台设备上 → 该设备；否则 → 当前会话 → 附近页选中的设备 → 唯一一台设备 → 弹出选择框。

**设置页**：查看本机信息、**已配对设备列表（可解绑）**、接收/数据目录、后台驻留与开机自启、防火墙工具与网络诊断。

**防火墙（Windows 最常见问题）**：热点网络通常被判定为「公用」profile，默认**拦截入站**，
表现为**能发现设备但发不出去**、且常是**单向**的（对方能发给你、你发不回去）。放行方式任选其一：

```powershell
# 1) 程序内：「设置」页 → 一键放行防火墙
# 2) 脚本（会自动请求管理员权限）
powershell -ExecutionPolicy Bypass -File scripts\allow_firewall.ps1
# 3) 手动（管理员 CMD），profile=any 才能覆盖热点所在的公用网络
netsh advfirewall firewall add rule name="Dasinong UDP 37020" dir=in action=allow protocol=UDP localport=37020 profile=any
netsh advfirewall firewall add rule name="Dasinong TCP 37021" dir=in action=allow protocol=TCP localport=37021 profile=any
```

---

## 打包

脚本在 `scripts/`，双击对应 `.bat`，或命令行调用 `.ps1`（`.bat` 会自动 `chcp 65001` 处理中文）。

```powershell
# 两端一起打包
powershell -ExecutionPolicy Bypass -File scripts\build_all.ps1

# 只打 Windows 端（PyInstaller，onedir）
powershell -ExecutionPolicy Bypass -File scripts\build_win.ps1
powershell -ExecutionPolicy Bypass -File scripts\build_win.ps1 -OneFile -Clean   # 单文件 exe
powershell -ExecutionPolicy Bypass -File scripts\build_win.ps1 -Console          # 带控制台便于排错

# 只打 Android 端
powershell -ExecutionPolicy Bypass -File scripts\build_android.ps1
powershell -ExecutionPolicy Bypass -File scripts\build_android.ps1 -Tests -Offline

# 出「可分发」的 release 包（Android 签名 APK + Windows zip）
powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1
```

**`build_release.ps1` 会自动处理签名密钥**：首次运行用 `keytool` 生成强随机口令的 PKCS12
keystore（RSA 4096）到 `大司农-android/keystore/`，把口令打印**一次**并另存备份；之后复用同一密钥。
打包后用 `apksigner` 校验签名。

> ⚠ `keystore/` 与 `keystore.properties` 已在 `.gitignore` 中，**绝对不要提交**。
> 丢了密钥就无法覆盖安装以后发布的版本（只能卸载重装）。

产物（统一 `DSN-` 前缀，与仓库名对齐）：

```
dist/release/
├── DSN-android-<版本>.apk      # 已签名
└── DSN-windows-<版本>.zip
```

版本号：Android 在 `大司农-android/UI/build.gradle.kts` 的 `versionName` / `versionCode`；
Windows 在 `大司农-win/UI/dasinong_ui/__init__.py` 与 `大司农-win/后端/dasinong_core/__init__.py` 的 `__version__`。
`build_release.ps1` 从 Android 的 `versionName` 取名，**改一处即可同时改两端产物名**。

---

## 协议

`protocol/PROTOCOL.md` 是**双端唯一事实源**。任何协议改动必须两端同步：

- **向后兼容**的改动（新增可选字段 / 新消息类型）**不递增** `PROTO_VERSION`，靠「忽略未知字段」规则兼容；
- **破坏性**改动才递增版本号。

当前 `PROTO_VERSION = 4`（`PROTO_MIN = 4`，即**不兼容 v3 及更早**，两端必须同时升级）：

- 单控制通道（TCP 长连接 JSON 帧）+ 每文件独立数据连接（分块）；
- 隧道 `dsn-tunnel v4`：TLS + **每设备独立身份** + **应用层双向 ECDSA 签名认证**（§2.5）；
- **首次连接配对**（6 位短码人工核对，§2.6）；未配对的 peer 无法收发业务数据。

---

## 安全说明

本软件面向**可信局域网**，且**加密始终开启**：每台设备首次运行会生成自己的**长期身份**
（随机 P-256 私钥，保存在本机且经系统加密：Windows 用 DPAPI、Android 用系统密钥库）。
**没有"口令"这个概念**，也没有明文模式。

- 每条 TCP 连接（控制 + 数据）都套 **TLS**（优先 1.3、可回退 1.2）。
- 身份认证在 **TLS 之上**再做一层**双向 ECDSA 签名**：双方各自用私钥对
  `方向 | 随机数 | 对端指纹` 签名，对方用它**声明的公钥**验签 —— 随机数防重放，对端指纹防中继。
- **首次连接需要配对**：两端会显示**同一个 6 位数字**，人工核对一致后各点一次「配对」；
  **两端都同意**才建立长期信任（不产生"半信任"）。之后长期有效，直到在设置里「解绑」。
- **未配对的设备发不了任何业务数据**（文本/文件都会被拒）。

> 界面**只显示那 6 位数字**，不显示任何指纹。
> 设备名 / 在线状态等元信息仍以明文广播（含设备名与 IP）。
> ⚠ 换机 / 重装 / 清数据 / 换 Windows 用户 → 本机身份换新 → 对端需要**重新配对**。

---

## 常见问题

**为什么要核对 6 位数字？不核对会怎样？**
这 6 位数字由**双方公钥 + 本次会话随机数**共同派生。如果有人在中间冒充，
他跟你和对方各握一次手时用的密钥不同，两端显示的数字就会**不一样** —— 一对就能发现。
点「配对」之前请务必确认两边数字完全相同。

**换手机 / 重装 / 清了数据之后，对方连不上了？**
本机身份是随机生成的，重装或清数据会换成一个新身份，对端会提示「身份已变更」。
在两端重新配对一次即可（会重新显示 6 位数字）。

**能发现设备，但消息 / 文件发不出去，或显示「未配对」**
两种情况：① 还没配对 —— 等配对弹窗出现并核对 6 位数字后两边都点「配对」；
② Windows 防火墙拦了入站 —— 见上文「防火墙」。发出去的消息会排队，连上后自动补发。

**手机开热点，两台设备互相发现不了**
Android 不向应用暴露热点网卡，广播发不出去 —— 本项目用 TCP 直连握手兜底。
若仍不行：确认确实连的是同一热点，并在手机 / 电脑上「手动添加对方 IP」。

**显示「版本过旧」**
两端协议版本不一致（本项目 v4 与 v3 不兼容）。把两端都升级到最新版即可。

**收到 .apk，点「打开」没反应**
系统需要你允许本应用「安装未知应用」：首次操作时系统会提示，去设置里开启即可。

**收到 `.apks`（分包安装包）**
系统安装器不认这种容器，需用 [SAI](https://github.com/Aefyr/SAI) 等支持分包安装的工具。
（分包应用只发 base APK 会装不上，所以自动打包成 `.apks`。）

**Android 构建报路径错误**
工程路径含非 ASCII 字符。保留 `android.overridePathCheck=true`，或把工程挪到纯 ASCII 路径。

---

## 第三方依赖与许可证

| 依赖 | 用途 | 许可证 |
|---|---|---|
| [PySide6](https://pypi.org/project/PySide6/) | Windows GUI | LGPL-3.0 |
| [psutil](https://pypi.org/project/psutil/) | 网卡 / 进程信息 | BSD-3-Clause |
| [cryptography](https://pypi.org/project/cryptography/) | 隧道 TLS / 自签证书 / ECDSA | Apache-2.0 或 BSD-3-Clause |
| [Pillow](https://pypi.org/project/Pillow/) | 打包时 JPG→ICO | MIT-CMU (HPND) |
| [pytest](https://pypi.org/project/pytest/) | 测试 | MIT |
| [Jetpack Compose](https://developer.android.com/jetpack/compose) / AndroidX | Android UI | Apache-2.0 |
| [Bouncy Castle](https://www.bouncycastle.org/)（`bcprov` / `bcpkix`） | P-256 点乘 / X.509 自签证书 / ECDSA | MIT 类（Bouncy Castle License） |
| [JUnit 4](https://junit.org/junit4/) | JVM 单测 | EPL-1.0 |

> 用 PyInstaller 打包 PySide6 分发时，请自行确认符合 **LGPL-3.0** 的分发要求
> （例如随附许可证文本、允许替换该库）。

---

## 许可证

本项目以 **GNU Affero General Public License v3.0（AGPL-3.0）** 发布，详见 [LICENSE](LICENSE)。

你可以自由使用、修改、分发本项目，但**衍生作品也必须以 AGPL-3.0 开源**；
并且**如果把它作为网络服务提供给他人使用，也必须向使用者提供完整源代码**。

## 免责声明

本项目仅供**可信局域网**内的个人文件互传使用，按「原样」提供，不附带任何明示或暗示的担保。
请自行评估网络环境与安全风险；请勿用于未获授权的网络或设备。
