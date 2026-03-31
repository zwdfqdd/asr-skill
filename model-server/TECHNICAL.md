# Model Download Auth Service — 技术文档

## 1. 系统概述

为 ONNX 模型文件提供安全分发能力。核心目标：

- 未授权用户无法下载模型
- 授权凭证泄露后风险可控
- 模型文件离开服务器后无法被复制到其他机器
- 客户端代码不暴露任何模型信息和服务地址

---

## 2. 架构

```
┌──────────────────────────────────────────────────────────────────┐
│                          服务端 (NAS)                            │
│                                                                  │
│   nginx (8080/HTTP, 443/TLS)                                     │
│   ├── /api/session/*    →  proxy → auth_middleware (8901)        │
│   ├── /files/*          →  auth_request /verify → 静态文件       │
│   ├── /admin/*          →  localhost only → auth_middleware      │
│   ├── /transcribe       →  proxy → asr_server (2701)            │
│   ├── /transcribe/base64→  proxy → asr_server (2701)            │
│   ├── /health           →  proxy → asr_server (2701)            │
│   └── /auth_verify      →  internal subrequest                  │
│                                                                  │
│   auth_middleware.py (port 8901)                                 │
│   ├── license_keys 表   →  长期凭证管理                          │
│   ├── sessions 表       →  临时会话管理                          │
│   ├── session_files 表  →  单文件下载追踪                        │
│   ├── download_log 表   →  审计日志                              │
│   └── manifest 生成     →  扫描 MODEL_FILES_ROOT                 │
│                                                                  │
│   asr_server.py (port 2701)                                      │
│   ├── POST /transcribe          →  multipart 音频文件上传        │
│   ├── POST /transcribe/base64   →  JSON base64 音频              │
│   ├── GET  /health              →  健康检查                      │
│   └── Pipeline: VAD → ASR → Punc (ONNX Runtime)                 │
│                                                                  │
│   /home/zhxg/zw/data/models      →  模型原始文件 (明文)          │
└──────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTPS
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                        客户端 (用户机器)                          │
│                                                                  │
│   download_models.py                                             │
│   ├── 读取 license.json  →  {license_key, endpoint}             │
│   ├── 采集 machine_fp    →  硬件指纹                             │
│   ├── POST /api/session/start                                    │
│   │   → 获取 session_token + manifest                            │
│   ├── GET /files/<path>?session=<token>                          │
│   │   → 下载到内存 → SHA-256 校验 → AES-256-GCM 加密 → 写 .enc  │
│   └── POST /api/session/complete                                 │
│       → 注销 session                                             │
│                                                                  │
│   model_crypto.py        →  运行时内存解密 → ONNX Runtime 加载   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. 双层凭证设计

### 3.1 为什么不用单 token

单 token 方案（v1）的问题：

| 风险 | 说明 |
|------|------|
| token 泄露 = 直接下载 | 拿到 token 就能下载全部模型 |
| 无法区分"身份"和"授权" | token 同时承担两个职责 |
| 长期有效 | 即使设了过期，窗口期内风险不可控 |
| 计数不精确 | nginx 缓存 auth 结果导致计数偏差 |

### 3.2 双层方案

```
license_key (长期)          session_token (临时)
─────────────────          ──────────────────────
证明"你是谁"                授权"这次下载"
管理员签发                  服务端自动生成
可设过期 (天级)             固定 TTL (30 分钟)
泄露后：不能直接下载        泄露后：很快过期
可绑定机器指纹              绑定到创建时的 IP
存储在 license.json         仅存在于下载过程中
```

### 3.3 流程时序

```
Client                                  Server
──────                                  ──────
POST /api/session/start
  {license_key, machine_fp}
                                        1. 查 license_keys 表，校验有效性
                                        2. 首次：绑定 machine_fp
                                           非首次：校验 machine_fp 一致
                                        3. 扫描 MODEL_FILES_ROOT 生成 manifest
                                        4. 创建 session (TTL 30min)
                                        5. license_keys.downloads += 1
                              ←         {session_token, expires_in, manifest}

GET /files/asr/model.onnx?session=xxx
  nginx → auth_request /verify
                                        6. 查 sessions 表，校验 status/过期
                                        7. 查 session_files，标记该文件 done
                                        8. 更新 bytes_done, bandwidth 计数
                                        9. 写 download_log
                              ←         200 + 文件流

  客户端：
  - 接收到内存
  - SHA-256 校验 vs manifest
  - AES-256-GCM 加密 (key = base_key + machine_fp)
  - 写 .onnx.enc 到磁盘

... (重复每个文件) ...

POST /api/session/complete
  {session_token}
                                        10. sessions.status = 'completed'
                              ←         {status: "completed"}

(如果客户端崩溃/断网)
                                        后台线程每 5 分钟扫描
                                        过期 session 自动标记 'expired'
```

---

## 4. 关键技术点

### 4.1 机器指纹 (machine_fp)

采集方式因平台不同：

| 平台 | 数据源 | 稳定性 |
|------|--------|--------|
| Windows | `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid` | 重装系统会变 |
| Linux | `/etc/machine-id` | 重装系统会变 |
| macOS | `IOPlatformUUID` (via ioreg) | 硬件级，非常稳定 |
| 所有平台 | `platform.node()` + `platform.machine()` | 改主机名会变 |

最终指纹 = `SHA-256(node | machine | platform_id)`

绑定逻辑：
- 首次使用 license_key 时，记录 machine_fp 到数据库
- 后续使用必须匹配，否则拒绝
- 一个 license_key 只能绑定一台机器

**注意**：如果用户需要换机器，管理员需要手动清除绑定（目前需直接操作数据库，后续可加 admin API）。

### 4.2 客户端加密

```
plaintext_model (内存)
        │
        ▼
base_key (32 bytes, models/model.key)
        │
        ├── machine_fp = SHA-256(硬件信息)
        │
        ▼
final_key = SHA-256(base_key + machine_fp)
        │
        ▼
AES-256-GCM(final_key, nonce=random_12bytes)
        │
        ▼
文件格式: MAGIC(4) + VERSION(1) + FLAGS(1) + NONCE(12) + CIPHERTEXT+TAG
          "OXEN"     0x01         0x01(绑定)
```

关键点：
- 明文模型只存在于内存中，从不写入磁盘
- `model.key` 是 base_key，不是最终密钥 — 即使 key 文件泄露，没有对应机器的硬件指纹也无法解密
- ONNX Runtime 支持从 bytes 加载模型：`ort.InferenceSession(decrypted_bytes)`

### 4.3 Manifest 与完整性校验

服务端扫描 `MODEL_FILES_ROOT` 生成 manifest：

```json
[
    {"path": "vad/vad.onnx", "size": 2412345, "sha256": "a1b2c3..."},
    {"path": "asr/model.onnx", "size": 262144000, "sha256": "d4e5f6..."},
    ...
]
```

- 客户端不硬编码任何文件名 — 全部从 manifest 动态获取
- 每个文件下载后立即 SHA-256 校验，不匹配则丢弃
- manifest 有 60 秒缓存，避免频繁磁盘扫描

### 4.4 Session 生命周期

```
                 create
                   │
                   ▼
              ┌─────────┐
              │  active  │
              └────┬─────┘
                   │
          ┌────────┼────────┐
          │        │        │
     complete   timeout   error
          │        │        │
          ▼        ▼        ▼
    ┌──────────┐ ┌─────────┐
    │completed │ │ expired │
    └──────────┘ └─────────┘
```

- `active`：正常下载中
- `completed`：客户端主动关闭（所有文件下载完成）
- `expired`：超过 TTL 未完成（后台线程清理）

### 4.5 nginx 配置要点

```
auth_request 不缓存
├── v1 缓存了 auth 结果 (proxy_cache_valid 200 5m)
│   → 导致 session 验证不实时，计数不准
└── v2 禁用缓存 (proxy_no_cache 1; proxy_cache_bypass 1)
    → 每次文件请求都实时校验 session

session_token 通过 query param 传递
├── X-Session header 由 nginx 从 $arg_session 提取
└── 客户端 URL: /files/<path>?session=<token>

admin 接口限 localhost
├── allow 127.0.0.1; deny all;
└── 防止外部直接访问管理 API
```

---

## 5. 重要参数

### 5.1 服务端环境变量

| 变量 | 必需 | 默认值 | 说明 |
|------|------|--------|------|
| `MODEL_ADMIN_KEY` | 是 | 自动生成(临时) | 管理 API 认证密钥 |
| `MODEL_FILES_ROOT` | 否 | `/home/zhxg/zw/data/models` | 模型文件根目录 |

### 5.2 服务端常量 (auth_middleware.py)

| 参数 | 值 | 说明 |
|------|-----|------|
| `HOST` | `127.0.0.1` | 只监听本地（nginx 反代） |
| `PORT` | `8901` | middleware 端口 |
| `SESSION_TTL` | `1800` (30min) | session 有效期 |
| `SESSION_CLEANUP_INTERVAL` | `300` (5min) | 过期清理间隔 |
| `DEFAULT_MAX_DOWNLOADS` | `100` | 默认最大下载会话数 |
| `DEFAULT_MAX_BANDWIDTH` | `10GB` | 默认最大带宽 |
| `DEFAULT_EXPIRE_DAYS` | `365` | 默认 license key 有效天数 |
| `_MANIFEST_TTL` | `60` (1min) | manifest 缓存时间 |

### 5.3 nginx 限流参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `model_download` | `10r/m burst=5` | 文件下载限流 |
| `session_api` | `3r/m burst=2` | session API 限流 |
| `asr_api` | `30r/m burst=10` | ASR 推理 API 限流 |
| `model_conn` | `3` | 最大并发连接 |

### 5.4 客户端 license.json

```json
{
    "license_key": "64位hex字符串",
    "endpoint": "https://your-server.example.com"
}
```

### 5.5 加密参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 算法 | AES-256-GCM | 认证加密 |
| base_key | 32 bytes (随机) | 存储在 `models/model.key` |
| nonce | 12 bytes (随机/每文件) | 存储在 .enc 文件头 |
| key 派生 | `SHA-256(base_key + machine_fp)` | 绑定机器 |
| 文件魔数 | `OXEN` (4 bytes) | 标识加密文件 |

---

## 6. 数据库结构

SQLite，文件 `auth.db`，WAL 模式。

### license_keys

| 字段 | 类型 | 说明 |
|------|------|------|
| key_hash | TEXT PK | SHA-256(license_key) |
| key_prefix | TEXT | 前 8 字符，用于显示 |
| label | TEXT | 标签（客户名等） |
| created_at | REAL | 创建时间戳 |
| expires_at | REAL | 过期时间戳 |
| max_downloads | INTEGER | 最大下载会话数 |
| max_bandwidth | INTEGER | 最大带宽 (bytes) |
| downloads | INTEGER | 已用会话数 |
| bandwidth | INTEGER | 已用带宽 (bytes) |
| machine_fp | TEXT | 绑定的机器指纹 |
| active | INTEGER | 1=有效, 0=已吊销 |

### sessions

| 字段 | 类型 | 说明 |
|------|------|------|
| session_token | TEXT PK | 48 位 hex |
| key_hash | TEXT FK | 关联 license_key |
| machine_fp | TEXT | 本次会话的机器指纹 |
| ip | TEXT | 客户端 IP |
| created_at | REAL | 创建时间 |
| expires_at | REAL | 过期时间 |
| files_total | INTEGER | manifest 文件总数 |
| files_done | INTEGER | 已下载文件数 |
| bytes_done | INTEGER | 已下载字节数 |
| status | TEXT | active/completed/expired |

### session_files

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| session_token | TEXT FK | 关联 session |
| file_path | TEXT | 文件相对路径 |
| file_size | INTEGER | 文件大小 |
| downloaded_at | REAL | 下载完成时间 |
| status | TEXT | pending/done |

### download_log

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| key_hash | TEXT | 关联 license_key |
| session_token | TEXT | 关联 session |
| timestamp | REAL | 时间戳 |
| ip | TEXT | 客户端 IP |
| uri | TEXT | 请求 URI |
| file_size | INTEGER | 文件大小 |
| status | TEXT | ok/error |

---

## 7. API 参考

### 7.1 公开接口

#### POST /api/session/start

开始下载会话。

请求：
```json
{
    "license_key": "64位hex",
    "machine_fp": "64位hex (SHA-256)"
}
```

成功响应 (200)：
```json
{
    "session_token": "48位hex",
    "expires_in": 1800,
    "manifest": [
        {"path": "vad/vad.onnx", "size": 2412345, "sha256": "..."},
        ...
    ]
}
```

错误响应 (403)：
```json
{"error": "key expired | key revoked | machine mismatch | download limit reached"}
```

#### GET /files/{path}?session={token}

下载单个文件。由 nginx 提供静态文件服务，auth_request 校验 session。

成功：200 + 文件流
失败：403 + `{"error": "reason"}`

#### POST /api/session/complete

关闭下载会话。

请求：
```json
{"session_token": "48位hex"}
```

响应：
```json
{"status": "completed"}
```

### 7.2 管理接口 (需 Authorization: Bearer $ADMIN_KEY)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /admin/tokens | 列出所有 license key |
| GET | /admin/stats | 统计信息 |
| GET | /admin/sessions | 最近 50 个 session |
| GET | /admin/manifest | 当前文件清单 |
| POST | /admin/tokens/create | 创建 license key |
| POST | /admin/tokens/revoke | 吊销 license key |

创建 license key：
```json
{
    "label": "customer1",
    "max_downloads": 50,
    "max_bandwidth": 10737418240,
    "expire_days": 90
}
```

---

## 8. 部署

### 8.1 前置条件

- Linux 服务器 (推荐 Ubuntu/Debian)
- Python 3.8+（无第三方依赖，纯 stdlib）
- nginx（已配置 TLS 证书）
- 模型文件已放置在 `MODEL_FILES_ROOT`

### 8.2 一键部署

```bash
cd model-server

# 部署 auth 中间件 (模型下载鉴权)
chmod +x deploy.sh
./deploy.sh

# 部署 ASR 推理服务
chmod +x deploy_asr_service.sh
./deploy_asr_service.sh
```

`deploy.sh` 自动完成：
1. 复制文件到 `/opt/model-auth/`
2. 生成 `MODEL_ADMIN_KEY`
3. 创建 systemd 服务 `model-auth`
4. 安装 nginx 配置
5. 生成首个 license key

`deploy_asr_service.sh` 自动完成：
1. 安装 Python 依赖 (onnxruntime, numpy, pyyaml, soundfile, websockets, cryptography)
2. 复制 ASR 脚本到 `/opt/asr-inference/`
3. 生成服务端 `asr_config.yaml` (模型路径指向 `/home/zhxg/zw/data/models`)
4. 创建 systemd 服务 `asr-inference`
5. 验证服务健康状态

### 8.3 手动部署

```bash
# 1. 准备模型文件
mkdir -p /home/zhxg/zw/data/models
# 将模型文件放入对应子目录

# 2. 安装 middleware
mkdir -p /opt/model-auth/logs
cp auth_middleware.py /opt/model-auth/

# 3. 设置环境变量
export MODEL_ADMIN_KEY=$(openssl rand -hex 16)
export MODEL_FILES_ROOT=/home/zhxg/zw/data/models

# 4. 启动
python3 /opt/model-auth/auth_middleware.py serve

# 5. 配置 nginx
cp nginx.conf /etc/nginx/conf.d/model-server.conf
# 编辑 SSL 证书路径
nginx -t && systemctl reload nginx
```

### 8.4 systemd 服务管理

```bash
# Auth 中间件
sudo systemctl status model-auth
sudo systemctl restart model-auth
journalctl -u model-auth -f

# ASR 推理服务
sudo systemctl status asr-inference
sudo systemctl restart asr-inference
journalctl -u asr-inference -f
```

### 8.5 nginx 配置检查

```bash
nginx -t                             # 语法检查
systemctl reload nginx               # 重载配置
tail -f /var/log/nginx/error.log     # 错误日志
```

---

## 9. 运维

### 9.1 日常管理

```bash
# 创建 license key
python3 auth_middleware.py create-token

# 列出所有 key
python3 auth_middleware.py list-tokens

# 吊销 key
python3 auth_middleware.py revoke <key>

# API 方式
ADMIN_KEY="your_admin_key"
curl -H "Authorization: Bearer $ADMIN_KEY" http://127.0.0.1:8901/admin/tokens
curl -H "Authorization: Bearer $ADMIN_KEY" http://127.0.0.1:8901/admin/sessions
curl -H "Authorization: Bearer $ADMIN_KEY" http://127.0.0.1:8901/admin/stats
```

### 9.2 数据库备份

```bash
# SQLite 在线备份 (不影响服务)
sqlite3 /opt/model-auth/auth.db ".backup /backup/auth-$(date +%Y%m%d).db"

# 或直接复制 (WAL 模式下安全)
cp /opt/model-auth/auth.db /backup/
cp /opt/model-auth/auth.db-wal /backup/   # 如果存在
```

### 9.3 日志

- middleware 日志：`/opt/model-auth/logs/auth-YYYY-MM-DD.log`
- nginx 访问日志：`/var/log/nginx/access.log`
- systemd 日志：`journalctl -u model-auth`

日志按天分割，建议配合 logrotate 定期清理。

### 9.4 监控要点

| 指标 | 获取方式 | 告警阈值 |
|------|----------|----------|
| 服务存活 | `systemctl is-active model-auth` | inactive |
| 活跃 session 数 | `GET /admin/stats → active_sessions` | 异常高 (>20) |
| 24h 下载量 | `GET /admin/stats → downloads_24h` | 异常高 |
| 磁盘空间 | `df -h /home/zhxg/zw/data/models` | >90% |
| auth.db 大小 | `ls -lh auth.db` | >100MB 考虑清理旧日志 |

### 9.5 故障排查

**客户端报 "machine mismatch"**
- 用户换了机器，或重装了系统导致指纹变化
- 解决：管理员清除绑定 `sqlite3 auth.db "UPDATE license_keys SET machine_fp='' WHERE key_prefix='xxxx'"`

**客户端报 "session expired"**
- 下载时间超过 30 分钟（大文件 + 慢网络）
- 解决：增大 `SESSION_TTL`，或优化网络

**客户端报 "download limit reached"**
- license key 的 `max_downloads` 已用完
- 解决：创建新 key，或 `sqlite3 auth.db "UPDATE license_keys SET downloads=0 WHERE key_prefix='xxxx'"`

**nginx 返回 403 但 middleware 日志无记录**
- nginx rate limit 触发（非 auth 拒绝）
- 检查 nginx error.log 中的 `limiting requests`

**manifest 为空**
- `MODEL_FILES_ROOT` 路径不对，或目录为空
- 检查：`ls -la /home/zhxg/zw/data/models`

---

## 10. 安全注意事项

1. **MODEL_ADMIN_KEY 务必保密** — 泄露等于可以任意创建 license key
2. **auth.db 包含所有凭证哈希** — 虽然是 SHA-256 不可逆，但仍应限制访问
3. **model.key (客户端)** — 丢失则模型无法解密，需重新下载
4. **TLS 必须启用** — license_key 和 session_token 通过 HTTPS 传输，HTTP 明文不安全
5. **admin 接口限 localhost** — nginx 配置了 `allow 127.0.0.1; deny all`，不要改
6. **服务端模型文件是明文** — 服务器本身的安全（SSH、防火墙）是最后一道防线
7. **SQLite 并发** — 当前用 `_db_lock` + WAL 模式，适合中低并发；高并发场景考虑换 PostgreSQL

---

## 11. 扩展方向

| 方向 | 说明 | 优先级 |
|------|------|--------|
| admin API: 解绑机器 | `POST /admin/tokens/unbind` 清除 machine_fp | 高 |
| admin API: 重置配额 | `POST /admin/tokens/reset-quota` | 高 |
| session 续期 | 下载大文件时自动延长 TTL | 中 |
| webhook 通知 | 下载完成/异常时回调 | 中 |
| 多机器绑定 | 一个 key 允许 N 台机器 | 低 |
| PostgreSQL 支持 | 高并发场景 | 低 |
| 增量更新 | 只下载变更的文件 | 低 |

---

## 12. 文件清单

```
model-server/
├── auth_middleware.py       # 鉴权中间件 (license + session + manifest)
├── nginx.conf               # nginx 反代配置 (auth + ASR 代理 + rate limit)
├── deploy.sh                # 一键部署 auth 中间件
├── deploy_asr_service.sh    # 一键部署 ASR 推理服务
├── assets/
│   └── asr_config.yaml      # ASR 推理默认配置
├── scripts/
│   ├── asr_server.py        # ASR HTTP/WebSocket 服务
│   ├── asr_tools.py         # ASR CLI (transcribe / batch / server)
│   ├── paraformer_onnx.py   # Paraformer 语音识别 ONNX 推理
│   ├── vad_onnx.py          # Silero VAD 语音活动检测
│   ├── punc_onnx.py         # 标点恢复 ONNX 推理
│   ├── model_crypto.py      # 模型加密/解密工具
│   └── test_asr.py          # 管线自测脚本
├── README.md                # 快速上手文档
└── TECHNICAL.md             # 本文件

asr_istarshine_v1/           # 客户端 skill (用户机器)
├── scripts/
│   ├── download_models.py   # 客户端下载器 (session-based)
│   ├── model_crypto.py      # 加密/解密工具
│   └── install.sh           # 安装脚本
├── models/
│   ├── model.key            # 加密密钥 (自动生成)
│   ├── vad/*.onnx.enc       # 加密模型
│   ├── asr/*.onnx.enc
│   └── punc/*.onnx.enc
├── license.json             # 客户端凭证 (用户创建)
└── references/
    └── MODELS.md            # 模型说明
```
