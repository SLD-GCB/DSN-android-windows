# 大司农（DSN）

局域网（含手机热点）下自动发现设备，**互传文件 / 图片 / 文件夹**，并支持**聊天式互发信息**。

**无服务器、不走互联网**：所有数据只在同一局域网内点对点传输。可选加密（口令派生的 TLS），加密与否由两端是否填了同一个口令决定。

> 从零自己实现的项目，定位与 [LocalSend](https://github.com/localsend/localsend) 同类（**不是它的 fork**）。
> 额外提供：聊天式收发文本、**并行分块 + 断点续传**、多设备同时收发、
> Android 端**分享已安装应用**、Windows 端**拖拽即发送**、传输记录持久化与清理。

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

**安全（可选）**
- 不设口令 = 明文；两端填**同一个口令** = 每条 TCP 连接套标准 TLS，身份由口令派生的自签证书公钥钉死
- 口令在界面上默认隐藏（可切换显示）；**任何界面都不显示本机指纹等敏感信息**

---

## 目录结构

```
DSN-android-windows/
├── protocol/PROTOCOL.md        # 协议唯一事实源（当前 PROTO_VERSION = 3）
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
python smoke_test.py          # 端到端自测：发现 / 文本 / 文件 / 文件夹 / 断点续传
python -m pytest tests -q     # 单元测试
```

单机跑两个实例联调（互不干扰）：

```bash
python run.py
DASINONG_TCP_PORT=37022 DASINONG_DATA_DIR=./_data_b python run.py
```

可用环境变量：`DASINONG_UDP_PORT` / `DASINONG_TCP_PORT` / `DASINONG_DATA_DIR` /
`DASINONG_RECEIVE_DIR` / `DASINONG_DEVICE_NAME` / `DASINONG_PREFS` / `DASINONG_PASSPHRASE`。

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

**附近页**：列出同一局域网内发现的设备，显示「已连接 / 未连接」。可对某台设备直接：打开会话、发文件、发图片、发文件夹、**分享应用**。

**会话页**：聊天式收发文本。输入框右侧圆形按钮发送；下方可发图片 / 文件 / 应用。标题栏「清空」可一键清空本会话记录；长按（手机）/ 右键（电脑）气泡可删除单条。

**传输页**：所有收发任务与进度。可打开文件、单条删除、一键清空（会连同已落盘文件一起清理；**进行中的任务会保留**）。

**Windows 拖拽**：把文件或文件夹**直接拖进窗口**即发送。目标按此顺序确定：
拖到某台设备上 → 该设备；否则 → 当前会话 → 附近页选中的设备 → 唯一一台设备 → 弹出选择框。

**加密口令**：两端「设置」填**同一个口令**即启用加密（会自动重连）。口令为空则不加密。
口令是「共享身份」——知道口令的人在你眼里就是「你的设备」。

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

判断是否连通：附近页每台设备都会显示「已连接 / 未连接」；发消息若显示「待发送」，说明还没建连（连上后会自动补发）。

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

产物：

```
dist/
├── win/大司农/大司农.exe                # onedir（默认），整目录拷走即可运行
├── android/大司农-android-debug.apk
├── release/大司农-android-<版本>.apk     # 已签名
├── release/大司农-windows-<版本>.zip
└── 大司农-windows.zip
```

版本号：Android 在 `大司农-android/UI/build.gradle.kts` 的 `versionName` / `versionCode`；
Windows 在 `大司农-win/UI/dasinong_ui/__init__.py` 与 `大司农-win/后端/dasinong_core/__init__.py` 的 `__version__`。
`build_release.ps1` 从 Android 的 `versionName` 取名，**改一处即可同时改两端产物名**。

---

## 协议

`protocol/PROTOCOL.md` 是**双端唯一事实源**。任何协议改动必须两端同步：

- **向后兼容**的改动（新增可选字段 / 新消息类型）**不递增** `PROTO_VERSION`，靠「忽略未知字段」规则兼容；
- **破坏性**改动才递增版本号。

当前 `PROTO_VERSION = 3`：单控制通道（TCP 长连接 JSON 帧）+ 每文件独立数据连接（分块）；
隧道握手 `dsn-tunnel v1`（口令 → PBKDF2 → P-256 身份 → 自签证书 → 公钥指纹钉死）。

---

## 安全说明

本软件面向**可信局域网**。加密是可选的，由「设置 → 隧道口令」决定：

- **两端填同一个口令** → 每条 TCP 连接套 **TLS**（优先 1.3，可回退 1.2），身份由
  **口令派生的自签证书公钥指纹**钉死：不知道口令的人连不上、也无法冒充；
  发现广播也带 HMAC 签名，口令不一致的两端**互相看不见**。
- **口令为空** → **明文 TCP**，同一网段内任何人可见、可发。

补充：

- 口令是**共享身份**，协议无法区分具体是哪一台设备。界面只显示「已加密 / 未加密」，
  **不显示本机指纹**；口令输入默认隐藏。
- 设备名、在线状态等元信息仍以明文广播（含设备名与 IP）。
- Android 端请求 `REQUEST_INSTALL_PACKAGES` 权限，仅用于「收到 .apk 后调起系统安装器」。
- 「分享应用」会读取已安装应用列表（`<queries>` 声明 MAIN/LAUNCHER），仅本机使用、不上传。

---

## 常见问题

**能发现设备，但消息 / 文件发不出去，或显示「待发送」**
几乎都是 Windows 防火墙拦了入站，见上文「防火墙」。发出去的消息会排队，连上后自动补发。

**手机开热点，两台设备互相发现不了**
Android 不向应用暴露热点网卡，广播发不出去 —— 本项目用 TCP 直连握手兜底。
若仍不行：确认确实连的是同一热点，并在手机 / 电脑上「手动添加对方 IP」。

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
| [cryptography](https://pypi.org/project/cryptography/) | 隧道 TLS / 自签证书 | Apache-2.0 或 BSD-3-Clause |
| [Pillow](https://pypi.org/project/Pillow/) | 打包时 JPG→ICO | MIT-CMU (HPND) |
| [pytest](https://pypi.org/project/pytest/) | 测试 | MIT |
| [Jetpack Compose](https://developer.android.com/jetpack/compose) / AndroidX | Android UI | Apache-2.0 |
| [Bouncy Castle](https://www.bouncycastle.org/)（`bcprov` / `bcpkix`） | P-256 点乘 / X.509 自签证书 | MIT 类（Bouncy Castle License） |
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
