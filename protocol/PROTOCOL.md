# 大司农 通信协议 v4

> 本文件是 Android 端与 Windows 端**唯一事实源**。任何协议改动必须两端同步，并递增 `PROTO_VERSION`（破坏性改动才递增；向后兼容的改动不动版本号，靠"忽略未知"规则）。

## 0. 设计目标
- 纯局域网、无服务器、不走互联网。
- 同一套协议支撑：**设备发现 / 聊天式文本 / 文件·图片·文件夹传输 / 断点续传**。
- **无互联网热点**（一机只开热点不联网）下依然可用。
- 向前兼容：接收方**必须忽略未知字段**、**跳过未知帧类型**。

## 1. 常量

| 名称 | 值 | 说明 |
|---|---|---|
| `PROTO_VERSION` | `5` | 协议版本（v5 = 端口池 `tcp_ports`/`data_ports` + 直连为先，**向后兼容**） |
| `PROTO_MIN` | `4` | 最低可互通版本；**v4↔v5 必须能互通**（缺端口池字段就退回单口） |
| `PROTO_MIN` | `4` | 本实现支持的最低版本（低于它 = 不兼容，只显示不拨号） |
| `UDP_DISCOVERY_PORT` | `37020` | 广播/多播/网关中继的 UDP 端口 |
| `TCP_PORT` | `37021` | 每设备 TCP 服务端口（控制 + 数据共用） |
| `MULTICAST_GROUP` | `224.0.0.167` | 多播回退组地址 |
| `HELLO_INTERVAL` | `3s` | 在线广播间隔（前台） |
| `HELLO_INTERVAL_BG` | `5s` | 后台广播间隔 |
| `PEER_TIMEOUT` | `10s` | 超过即判定离线 |
| `PING_INTERVAL` | `3s` | 控制连接保活间隔 |
| `PING_TIMEOUT` | `6s` | 2× 无 pong → DEGRADED |
| `CONNECT_TIMEOUT` | `5s` | TCP 建连超时 |
| `RECONNECT_BACKOFF` | `0.5→1→2→4→8→15s` | 指数退避（±20% jitter，封顶 15s） |
| `GATEWAY_PROBE_INTERVAL` | `5s` | 网关 hello 周期 |
| `HISTORY_PROBE_INTERVAL` | `15s` | 历史 IP 探测周期 |
| `CHUNK_SIZE` | `65536` | 默认分块大小（可协商 256KiB / 1MiB） |
| `MAX_FRAME` | `1048576` | 单帧上限，防畸形长度 |
| `DEFAULT_PARALLEL` | `3` | 单文件默认并行数据连接数（上限 8） |
| `DEFAULT_WINDOW_BYTES` | `4194304` | 应用级信用窗口（4 MiB） |
| `DATA_IDLE_CLOSE` | `5s` | 数据连接空闲关闭 |

端口可用环境变量覆盖（Windows）：`DASINONG_UDP_PORT` / `DASINONG_TCP_PORT`。

## 2. TCP 帧格式

所有 TCP 报文（控制与数据）统一为：

```
+------------------+----------+---------------------------+
| u32 len (BE)     | u8 type  | body (len-1 bytes)        |
+------------------+----------+---------------------------+
len = len(body) + 1
```

`type` 取值：

| type | 名称 | body |
|---|---|---|
| `0x01` | `JSON` | UTF-8 JSON（控制报文，见 §4/§5） |
| `0x03` | `AUTH_HELLO` | 隧道握手：`{"nonce":"<hex>"}`（见 §2.5） |
| `0x04` | `AUTH_REQ` | 隧道握手：`{"mac":"<hex>"}` |
| `0x05` | `AUTH_RES` | 隧道握手：`{"mac2":"<hex>"}` |
| `0x06` | `AUTH_ERR` | 隧道握手失败：`{"错":"..."}` |
| `0x02` | `DATA_HELLO` | 见 §6.1 |
| `0x10` | `CHUNK` | 见 §6.2 |
| `0x11` | `CHUNK_ACK` | 见 §6.3 |
| `0x12` | `DATA_DONE` | 见 §6.4 |
| `0x7F` | `DATA_ERROR` | 见 §6.5 |

规则：
- 读 `len` 后若 `len > MAX_FRAME` 或 `len < 1` → 视为协议错误，断开连接。
- 未知 `type` → 读满 `len-1` 字节后**丢弃该帧**并继续读下一帧。

## 2.5 传输层安全（加密隧道 dsn-tunnel v4）

**密码学一行不自己造**：加密用标准 **TLS**（Python `ssl` / Android `SSLSocket`），
优先 TLS 1.3、允许回退 1.2（Android 26~28 最高只到 1.2）。

**身份 = 每设备独立密钥**（v4 起，**不再由口令派生**）：

| 项 | 值 |
|---|---|
| 身份 | 首次运行生成 **32 字节随机种子** → P-256 私钥 |
| 标量 | `d = (int(seed) mod (n-1)) + 1`（`n` = P-256 阶） |
| 证书 | 自签，CN `dasinong-identity`，SHA256withECDSA，随机序列号，10 年 |
| 指纹 | **SPKI DER 的 SHA-256**（小写 hex）。⚠ 不是整张证书的哈希 |
| 私钥保护 | Windows：DPAPI（绑定当前用户）；Android：系统密钥库 AES-GCM |

**两侧都不预知对端指纹**（对端身份事先不知道），所以：

- TLS 层**不校验证书链**（它只负责机密性与完整性）；
- 真正的身份证明放在**应用层**：双方各自用自己的私钥对 `方向|nonce|对端指纹` 签名，
  对方用**它声明的公钥**验签。nonce 防重放、对端指纹防中继/降级。

```
服务端 → DSN1                       (4 字节 magic)
服务端 → AUTH_HELLO {nonce, fp}      (0x03)   fp = 服务端自报的指纹
客户端 → AUTH_REQ  {fp, pub, sig}    (0x04)   sig = ECDSA(client_priv, "client|" + nonce + "|" + fp_server)
服务端 → AUTH_RES  {fp, pub, sig}    (0x05)   sig = ECDSA(server_priv, "server|" + nonce + "|" + fp_client)
服务端 → AUTH_ERR  {code, 错}        (0x06)   握手/身份校验失败
```

- `pub` 是 base64 的 SPKI DER；接收方必须校验 `SHA256(pub) == fp`，否则拒绝。
- 客户端会把服务端**自报的 fp** 纳入自己的签名，服务端核对自己是否真是它 → 防中继。

**握手只证明"你持有你自称的那把钥匙"**；是否**信任**由**配对表**决定（见 §2.6）。

**没有明文模式**：本机拿不到身份/隧道就**拒绝连接**（fail-closed），绝不退化成明文 TCP。

## 2.6 配对（首次连接的人工确认）

配对表（`paired`）以 `device_id` 为主键，存对端指纹、名字、首/末次时间与 `trusted`。

- 控制通道建立后，**拨号方**发 `pair_request{name}`；两端各自算出**同一个 6 位短码**：

```
digest = SHA-256("dasinong-sas-v1" || 较小指纹 || 较大指纹 || nonce)   # 指纹取原始 32 字节
code   = 大端无符号(digest[0:4]) % 1_000_000        # 左补零到 6 位
```

- 两端弹窗显示该短码；**核对一致**后各点一次「配对」（各自回 `pair_accept`）；
  **两端都同意**才写 `paired`（不产生"半信任"），随后互发 `pair_ok`。
- 任一端取消 → `pair_reject` → 断开；60 秒无人确认 → 视为拒绝。
- **未配对的 peer，业务帧一律拒绝**（回 `error{code:"unpaired"}`，仅放行 `ping/pong` 与配对报文）。
- 解绑：删本机那条记录（**单侧语义**），下次连接重新配对。

> ⚠ 界面**只显示这 6 位短码**，不显示任何指纹（用户硬要求）；短码是本次会话的临时值。

## 3. JSON 信封

UDP 发现报文、网关中继报文、TCP 控制报文共用同一信封：

```json
{
  "proto": 2,
  "type": "hello",
  "id": "8f2c...uuid",
  "from": "b1a2...uuid",
  "to": null,
  "ts": 1730000000123,
  "data": {}
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `proto` | int | 发送方协议版本 |
| `type` | string | 报文类型 |
| `id` | string | 本消息唯一 UUID，用于去重/回执 |
| `from` | string | 发送方 `device_id` |
| `to` | string\|null | 目标 `device_id`；广播/多播时为 `null` |
| `ts` | int | epoch 毫秒 |
| `data` | object | 类型相关载荷 |

**device_id**：UUIDv4，持久化到数据目录 `.device_id`，同一次安装内稳定。
**device_name**：默认主机名，可覆盖。
接收方一律以**数据包源 IP** 作为该设备的 `ip`（`data` 内的 `ip` 仅作提示）。

## 4. 发现报文（UDP，端口 37020）

### 4.1 `hello`
```json
{"proto":3,"type":"hello","id":"...","from":"...","to":null,"ts":0,
 "data":{"name":"小明-手机","device_type":"android","tcp_port":37021,
         "udp_port":37020,"proto_min":1,"proto_max":3,"ts":0,
         "enc":1,"mac":"<hex>"}}
```
`device_type` ∈ `android` | `windows`。

**`enc` / `pub` / `sig`（v4）**：`enc=1` 表示本机已启用加密；`pub` 是 base64 的 **SPKI DER**，
`sig` = `ECDSA-SHA256(设备私钥, "from|name|device_type|tcp_port|udp_port|ts")`（字段固定、`|` 连接，
见 `network/tunnel.py::hello_fields`）。接收方**必须**用 `pub` 验 `sig`，验不过就丢弃
（防同网段伪造设备冒进列表）；`sig` 覆盖 `from`，顺带防了 `device_id` 冒用。

### 4.2 `bye`
```json
{"proto":2,"type":"bye","id":"...","from":"...","to":null,"ts":0,"data":{}}
```
优雅退出时广播，接收方立即标记离线。

## 5. 网关中继报文（UDP 单播，端口 37020）

用于**无互联网热点 / AP 隔离**场景：客户端↔客户端广播可能被拦，改由"客户端 → 网关（热点主机）"单播汇聚。

`device_info = {device_id, name, device_type, ip, tcp_port, udp_port, proto, ts}`

| type | 方向 | data |
|---|---|---|
| `gw_hello` | 客户端→网关 | `{}`（探测网关是否有大司农主机） |
| `gw_ack` | 网关→客户端 | `{"host": device_info}` |
| `gw_register` | 客户端→网关 | `{"device": device_info}` |
| `gw_peer_list` | 网关→客户端 | `{"peers": [device_info, ...]}` |
| `gw_goodbye` | 客户端→网关 | `{"device_id": "..."}` |

**候选网关表**（客户端周期探测，同时叠加真实默认网关）：
`192.168.43.1`(Android 热点) / `192.168.42.1` / `192.168.137.1`(Windows 移动热点) / `172.20.10.1`(iOS) / 本机默认网关。

## 6. 数据通道（TCP，按需短连接）

同一文件可开 N 条并行数据连接。每条连接建立后**首帧必须是 `DATA_HELLO`**，随后只发二进制块。

### 6.1 `DATA_HELLO` (0x02)
```
u16 dev_id_len + dev_id(utf8) +
u16 transfer_id_len + transfer_id(utf8) +
u16 file_id_len + file_id(utf8)
```

### 6.2 `CHUNK` (0x10)
```
u64 offset (BE) + payload(raw bytes, ≤ chunk_size)
```

### 6.3 `CHUNK_ACK` (0x11)
```
u64 cumulative_offset (BE)
```

### 6.4 `DATA_DONE` (0x12)
```
u64 total_size (BE) + 32B sha256(raw)
```

### 6.5 `DATA_ERROR` (0x7F)
```
u16 code (BE) + utf8 message
```

## 7. 控制通道（TCP 长连接，JSON 帧）

每对设备维持**一条持久控制连接**，只跑 JSON。由 `device_id` 字典序较小的一方主动发起，避免重复建连。

### 7.1 握手
首帧：
```json
{"proto":3,"type":"connect","id":"...","from":"...","to":"...","ts":0,
 "data":{"role":"control",
         "device_id":"<拨号方>","name":"客厅手机","device_type":"android",
         "tcp_port":37021,"udp_port":37020,"proto":5,
         "tcp_ports":[37021],"data_ports":[37021]}}
```

`data` 里**始终**带上拨号方自己的身份（`device_id` 与 `from` 相同，其余为展示/端口信息）。

> 为什么要带：被叫方收到连接后要把拨号方**当前**的地址和端口登记下来，否则它"能收、
> 发不回去"—— 传文件的数据连接需要知道往哪发。热点场景下（手机开热点时 Android 不暴露
> 热点网卡）对端**根本发不出 UDP 广播**，这条入站连接是唯一的信息来源。
> 老版本读不懂就忽略多余字段（§0 规定未知字段/类型一律忽略），不影响兼容。

**直连（`ip_only`）**：当拨号方**只知道对方 IP、还不知道 device_id** 时（典型场景：手机开热点，Android 不暴露热点网卡，UDP 发现走不通），`to` 留 `null` 并在 `data` 里加 `ip_only: true`：

```json
{"proto":3,"type":"connect","id":"<拨号方>","from":"<拨号方>","to":null,"ts":0,
 "data":{"role":"control","ip_only":true}}
```

被叫方收到后照常建会话（对端 id 从 `from` 取），**并立刻回一条身份应答** `connect_ok`（仅当 `ip_only` 为真时才回）：

```json
{"proto":3,"type":"connect_ok","id":"<被叫>","from":"<被叫>","to":"<拨号方>","ts":0,
 "data":{"device_id":"<被叫>","name":"客厅手机","device_type":"android",
         "tcp_port":37021,"udp_port":37020,"proto":3}}
```

拨号方**必须在创建会话之前**先读这一帧，拿到真实 `device_id` 后再 `_register`——这样两端都无需在会话建立后改写 peer_id。

- `ip_only` 是**可选字段**，不升 `PROTO_VERSION`；被叫方只在它为真时回包，因此普通发现路径的字节流完全不变。
- 老版本被叫方不会回 `connect_ok`：拨号方必须**短超时兜底**，报错并回退到原来的 UDP 发现路径（不能死等）。
- 老版本拨号方收到不认识的 `connect_ok` 按 §0/§2 规则**忽略**。

### 7.2 控制报文清单

| type | 方向 | data 关键字段 | 说明 |
|---|---|---|---|
| `connect` | 双向 | `role`, `ip_only?` | 首帧握手；`ip_only` 表示「只知 IP 的直连」 |
| `connect_ok` | 被叫→拨号 | `{device_id, name, device_type, tcp_port, udp_port, proto}` | 直连时回的身份应答（仅 `ip_only` 为真时发） |
| `text` | 双向 | `{text, client_id}` | 聊天文本；`client_id` 去重 |
| `delivered` | 收→发 | `{msg_id}` | 送达回执 |
| `offer` | 发→收 | `{transfer_id, files:[...]}` | 传输提议（见 §7.3） |
| `accept` | 收→发 | `{transfer_id, files:[...], max_parallel, window_bytes}` | 同意 + 续传区间（见 §7.4） |
| `reject` | 收→发 | `{transfer_id, reason}` | 拒绝 |
| `resume_ack` | 发→收 | `{transfer_id, files:[{file_id, start_offset}]}` | 采纳续传 |
| `window_update` | 收→发 | `{transfer_id, file_id, acked_offset}` | 信用窗口更新 |
| `chunk_ack` | 收→发 | `{transfer_id, file_id, cumulative_offset}` | 累计确认 |
| `progress` | 双向 | `{transfer_id, file_id, sent, received, total}` | 进度（节流 ≥200ms 或 ≥1%） |
| `complete` | 发→收 | `{transfer_id, file_id}` | 某文件发送完毕 |
| `done` | 收→发 | `{transfer_id, file_id}` | 校验并落盘完成 |
| `cancel` | 双向 | `{transfer_id, file_id?, reason}` | 取消 |
| `error` | 双向 | `{transfer_id?, file_id?, code, message}` | 错误；`code` 可为 `proto`（版本不兼容）/`unpaired`（未配对，见 §2.6） |
| `pair_request` | 拨号→被叫 | `{name}` | 请求配对（见 §2.6） |
| `pair_accept` | 双向 | `{}` | 本端用户同意配对 |
| `pair_reject` | 双向 | `{}` | 本端用户拒绝配对（断开） |
| `pair_ok` | 双向 | `{fp}` | 本端已落库，通知对端完成 |
| `ping` / `pong` | 双向 | `{}` | 保活 |

### 7.3 `offer.files[]` 单项
```json
{
  "file_id": "uuid",
  "name": "报告.pdf",
  "rel_path": "项目/2026/报告.pdf",
  "root_name": "项目",
  "size": 123456789,
  "kind": "file",
  "content_id": "sha256-hex",
  "chunk_size": 65536,
  "total_chunks": 1884,
  "preview": null,
  "mtime": 1728600000
}
```
- `kind` ∈ `file` | `image` | `text`。
- `image` 时 `preview` 为 ≤64KiB 的 base64 JPEG 缩略图。
- `rel_path`：相对路径（用 `/` 分隔），**支撑文件夹传输**；单文件时等于 `name`。
- `root_name`：文件夹名（用于接收端建顶层目录）；单文件为 `null`。
- `content_id`：内容 sha256，**续传判定依据（content_id + size），不依赖文件名**。

### 7.4 `accept.files[]` 单项
```json
{
  "file_id": "uuid",
  "accepted": true,
  "resume": true,
  "received_ranges": [[0,131071],[262144,327679]]
}
```
`received_ranges`：接收端已落盘的**字节区间**（闭区间，已合并相邻）。

## 8. 传输时序

```
A(发送)                                         B(接收)
  |-- offer(transfer_id, files[]) ------------->|
  |<------------- accept(files[].received_ranges, max_parallel) --|
  |-- resume_ack(可选) ------------------------->|
  |==== 打开 N 条数据连接 DATA_HELLO ===========>|
  |-- CHUNK(offset,payload) ------------------->|  随机写 .part
  |<------------- progress / window_update -----|  (节流)
  |-- DATA_DONE(size, sha256) ----------------->|
  |-- complete(transfer_id, file_id) ---------->|
  |<------------- done(transfer_id, file_id) ---|  fsync + 校验 + 原子改名
```

## 9. 续传与完整性

- 接收端先写 `xxx.part`，同时原子维护 `xxx.partmeta`：
  ```json
  {"transfer_id":"...","file_id":"...","name":"报告.pdf","size":123456789,
   "sha256":"hex","chunk_size":65536,"total_chunks":1884,
   "received":[[0,131071]],"updated_at":1728600000}
  ```
- 重连/重启后，发送方发 `offer`，接收端若匹配 `content_id+size` 且有 `.partmeta` → 回 `accept{resume:true, received_ranges}`；发送方跳过已收区间并回 `resume_ack`。
- 收满后：`fsync` → 校验整文件 sha256 → 原子改名（Windows `os.replace` / Android `renameTo`）。
- 块级失败重试 ≤3 次；严重错误发 `error` 终止该文件。

## 10. 图片 / 文件 / 文件夹

- `kind=image`：`mime` 以 `image/` 开头且体积适合预览；带 base64 缩略图。
- 文件夹：一个 `offer` 内多个 `files[]`，靠 `rel_path` + `root_name` 重建目录树。
- 接收端**路径清洗**：UTF-8/NFC 归一化、替换非法字符 `\ / : * ? " < > |`、去 Windows 保留名（`CON/PRN/AUX/NUL/COM1..`）、去结尾点/空格、限长、**拒绝 `..` 路径穿越**。

## 11. 版本协商

- `hello` 携带 `proto`、`proto_min`、`proto_max`；`connect` 信封也带 `proto`。
- 有效版本 = `min(local.proto, remote.proto_max)`，且须 `>= remote.proto_min` 且 `>= local.proto_min`；
  否则视为**不兼容**。
- **不兼容的处理（已实现，不再是纸面约定）**：
  1. 发现层把该 peer 登记进列表并标 `incompatible=true`，**不拨号**，UI 显示"需升级"；
  2. 拨号侧对已知过旧的对端直接返回可读错误；
  3. 入站收到 `connect` 时若版本不兼容 → 回 `error{code:"proto"}` 并断开，不建会话。
- 向后兼容改动（新增可选字段/报文类型）**不**递增 `proto`；破坏性改动才递增，
  且两端必须**同时升级**（v4 起 `PROTO_MIN = 4`，即不兼容 v3 及更早）。

## 12. 安全模型

- **强制加密**：引擎在**未设置口令时不启动任何网络功能**（不监听、不广播）。
  也就是说本协议不存在可用的明文模式；"口令为空 = 明文"只保留在隧道模块内部，
  供单元测试构造环回使用，**运行期不会被走到**。
- 加密与身份见 §2.5（TLS + 口令派生身份 + 公钥指纹钉死 + AUTH 双向认证）。
- 发现报文在口令非空时带 HMAC 签名，**口令不一致的两端互相看不见**（§4.1）。
- 已知局限（v3）：口令是**共享身份**（同口令 = 同指纹），协议无法区分具体哪一台设备；
  设备名 / IP / 在线状态等元信息仍以明文广播。想区分设备需每设备独立身份（计划中的 v4）。
- **每设备独立身份** + **首次配对确认**（6 位短码人工核对，见 §2.6）：能区分具体哪台设备、可解绑；
  未配对的 peer 无法收发业务数据。
- 私钥以**加密形式**保存在本机（Windows：DPAPI 绑定当前用户；Android：系统密钥库），不落明文。
- ⚠ 换机 / 重装 / 清数据 / 换 Windows 用户 → 身份换新 → 对端需**重新配对**（会提示「身份已变更」）。


## 9. 端口池（v5）

**字段**（出现在**所有身份通告**里：`connect` 的信封 `data`、`connect_ok`、`hello`）：

| 字段 | 含义 |
| --- | --- |
| `tcp_ports` | **控制连接**可用端口池（数组；首项即主口 `tcp_port`） |
| `data_ports` | **数据连接**可用端口池（数组） |

**规则**

1. **直连为先**：发现/连接**一律用 TCP 直连**（安卓与 Windows 的网卡/广播行为不一致，广播不可依赖）。
   UDP 广播只作**可选加速**，任何"必须先收到广播才能工作"的路径都不允许。
2. **选口**：拨号方从对端 `tcp_ports` 里挑**占用最少**的口连控制连接；
   数据连接按 `data_ports` 并发开多条（并发数见设置页「TCP 并发」）。
3. **回退（必须）**：对端**没有**这两个字段 → 退回单口 `tcp_port`（老版本照常互通）。
   同理 `data_ports` 缺失时，数据走原有单口隧道。
4. **版本**：`PROTO_VERSION = 5`、`PROTO_MIN = 4`；两端 `negotiate` 允许 **4↔5 互通**。

**设置页（两端）**

| 项 | 类型 | 默认 | 作用 |
| --- | --- | --- | --- |
| 「TCP 并发」 | 数字（1~8） | 3 | 并发几条数据连接；填 1 = 单连接 |
| 「UDP 传输」 | 开关 | 关 | **大文件走 UDP 直连**（点对点，不做广播）；需自建可靠层（分片/重传/排序/限速） |
