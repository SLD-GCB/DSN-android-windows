# 大司农 通信协议 v3

> 本文件是 Android 端与 Windows 端**唯一事实源**。任何协议改动必须两端同步，并递增 `PROTO_VERSION`（破坏性改动才递增；向后兼容的改动不动版本号，靠"忽略未知"规则）。

## 0. 设计目标
- 纯局域网、无服务器、不走互联网。
- 同一套协议支撑：**设备发现 / 聊天式文本 / 文件·图片·文件夹传输 / 断点续传**。
- **无互联网热点**（一机只开热点不联网）下依然可用。
- 向前兼容：接收方**必须忽略未知字段**、**跳过未知帧类型**。

## 1. 常量

| 名称 | 值 | 说明 |
|---|---|---|
| `PROTO_VERSION` | `3` | 协议版本 |
| `PROTO_MIN` | `1` | 本实现支持的最低版本 |
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
| `0x03` | `AUTH_HELLO` | 隧道握手：`{"nonce":"<hex>"}`（见 §0.5） |
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

## 2.5 传输层安全（加密隧道 dsn-tunnel v3）

设计参照 honglu-tunnel v1（`ai酒馆/酒馆隧道.py` + `鸿胪寺/后端/隧道.kt`）：**密码学一行不自己造**。

**加密**：每条 TCP 连接（控制 + 数据）套标准 **TLS**（Python `ssl` / Android `SSLSocket`）。
优先 TLS 1.3，允许回退 1.2（Android 26~28 最高只到 1.2）。

**身份**：口令派生（两端**同一口令 → 同一把密钥 → 同一指纹**），无需扫码/比对/手输指纹。

| 项 | 值 |
|---|---|
| 盐 | `dasinong-tunnel-v1` |
| KDF | PBKDF2-HMAC-SHA256，**200000** 次迭代，输出 32B |
| 曲线 | P-256 (secp256r1)；标量 `d = (int(材料) mod (n-1)) + 1` |
| 证书 | 自签，CN `dasinong-tunnel`，SHA256withECDSA，随机序列号，10 年 |
| 指纹 | **SPKI DER 的 SHA-256**（小写 hex）。⚠ 不是整张证书的哈希 |

客户端侧**只认这个公钥指纹**：口令不一致的人连不上，也冒充不了。
口令为空 → 不加密（见下）。

**TLS 之上再做一道 HMAC 双向认证**（用于给出明确的「口令不一致」报错）：
```
服务端 → DSN1                      (4 字节 magic)
服务端 → AUTH_HELLO {nonce}        (0x03)
客户端 → AUTH_REQ  {mac}           (0x04)   mac  = HMAC-SHA256(k_auth, "client|" + nonce)
服务端 → AUTH_RES  {mac2}          (0x05)   mac2 = HMAC-SHA256(k_auth, "server|" + nonce)
```
握手通过后才允许发业务首帧（`connect` / `DATA_HELLO`）。

**口令密钥（域分离）**：
`key(用途) = PBKDF2-HMAC-SHA256(口令, 盐 + "|" + 用途, 200000, 32)`
  · 用途 `hello` → 发现报文签名。⚠ 广播是**明文**，必须走 PBKDF2，否则等于把
    "口令原文做 HMAC"暴露成可离线爆破的口令预言机。
  · 用途 `auth`  → AUTH 握手。
身份派生用**不带用途后缀**的 `PBKDF2(口令, 盐)`。

**口令为空 = 不加密**：不套 TLS、不跑握手，走明文 TCP。
本机口令非空时会忽略未签名的发现报文（见 §4.1），所以口令不一致的两台设备**互相看不见**。

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

**`enc` / `mac`（v3 新增，仅当本机口令非空时携带）**：
`mac = HMAC-SHA256(口令, "from|name|device_type|tcp_port|udp_port|ts")`，
字段顺序固定、以 `|` 连接（见 `network/tunnel.py::hello_fields`）。
接收方口令非空时，**未带有效 mac 的 hello 一律忽略**（防同网段伪造设备冒进列表）；
口令为空时不做校验（兼容明文模式）。

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
         "tcp_port":37021,"udp_port":37020,"proto":3}}
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
| `error` | 双向 | `{transfer_id?, file_id?, code, message}` | 错误 |
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

- `hello` 携带 `proto`、`proto_min`、`proto_max`。
- 有效版本 = `min(a.proto, b.proto)`，且须落在对方 `[proto_min, proto_max]` 内。
- 若 `remote.proto_max < local.proto_min` → 标记 peer 不兼容，禁用传输并在 UI 提示。
- 向后兼容改动（新增可选字段/报文类型）**不**递增 `proto`。

## 12. 安全（v1/v2 说明）

- 无鉴权、开放局域网；仅响应同子网来源，避免误连外网。
- README/UI 明示"同网段任何人可见/可发"。
- 预留升级点：共享 PIN 派生 HMAC 校验 `hello`/`connect`，或 TLS（届时递增 `proto`）。
