# Model Download Auth Server (v0.2.0)

保护模型下载源，防止未授权访问。支持外网/内网下载、多机器绑定、增量更新。

## 架构

```
Client (download_models.py)
  │
  │  POST /api/session/start
  │  {license_key, machine_fp}
  ▼
nginx (TLS + rate limit)
  │
  ▼
auth_middleware.py (port 8901)
  │  验证 license_key
  │  绑定/校验 machine_fp
  │  生成 session_token (TTL 30min)
  │  返回 {session_token, manifest}
  ▼
Client
  │  GET /files/<path>?session=<token>
  │  (每个文件逐一下载)
  ▼
nginx → auth_request /verify
  │  校验 session_token → 有效则放行
  ▼
返回文件 → 客户端内存加密 → 写 .enc
  │
  │  POST /api/session/complete
  │  session_token 注销
  ▼
Done
```

## 双层凭证设计

| 凭证 | 生命周期 | 作用 | 泄露风险 |
|------|----------|------|----------|
| license_key | 长期 (可设过期) | 证明身份 | 不能直接下载，需配合机器指纹 |
| session_token | 临时 (30min, 自动续期) | 授权本次下载 | 过期自动失效，用完即销毁 |

## 安全层级

| 层 | 保护 | 说明 |
|---|------|------|
| 双层凭证 | 防凭证泄露 | license_key 不能直接下载，session_token 临时有效 |
| 机器指纹绑定 | 防模型拷贝 | 首次使用绑定机器，换机器拒绝 |
| 文件 manifest | 防篡改 | SHA-256 校验每个文件完整性 |
| 客户端内存加密 | 防明文落盘 | 下载→内存→AES-256-GCM→写 .enc |
| nginx rate limit | 防爬/防滥用 | 30次/分钟，5并发 |
| session 自动过期 | 防遗留授权 | 30min 超时，活跃下载自动续期 |

## 快速部署

```bash
# 在 nas.istarshine.com 服务器上
chmod +x deploy.sh
./deploy.sh
```

自动完成：安装文件、生成 admin key、创建 systemd 服务、配置 nginx、生成首个 license key。

## 手动部署

### 1. 准备模型文件

```bash
# 将模型文件放到 /home/zhxg/zw/data/models 下
# 目录结构会自动扫描生成 manifest
/home/zhxg/zw/data/models/
├── vad/vad.onnx
├── asr/model.onnx
├── asr/tokens.json
├── asr/am.mvn
└── punc/model.onnx
    ...
```

### 2. 启动 auth 中间件

```bash
export MODEL_ADMIN_KEY=$(openssl rand -hex 16)
export MODEL_FILES_ROOT=/home/zhxg/zw/data/models
python3 auth_middleware.py serve
```

### 3. 配置 nginx

```bash
cp nginx.conf /etc/nginx/conf.d/model-server.conf
# 编辑 SSL 证书路径
nginx -t && systemctl reload nginx
```

### 4. 创建 license key

```bash
# 交互式
python3 auth_middleware.py create-token

# API
curl -X POST -H "Authorization: Bearer $MODEL_ADMIN_KEY" \
     -H "Content-Type: application/json" \
     -d '{"label":"customer1","max_downloads":50,"expire_days":90}' \
     http://127.0.0.1:8901/admin/tokens/create
```

### 5. 客户端配置

在 skill 目录下创建 `license.json`：

```json
{
    "license_key": "your_key_here",
    "endpoint": "https://nas.istarshine.com"
}
```

下载模型：

```bash
python download_models.py
# 或指定参数
python download_models.py --license-key <key> --endpoint https://nas.istarshine.com
```

## 管理命令

```bash
# 列出所有 license key
python3 auth_middleware.py list-tokens

# 吊销 license key
python3 auth_middleware.py revoke <key>

# API: 查看 keys
curl -H "Authorization: Bearer $ADMIN_KEY" http://127.0.0.1:8901/admin/tokens

# API: 查看活跃 sessions
curl -H "Authorization: Bearer $ADMIN_KEY" http://127.0.0.1:8901/admin/sessions

# API: 查看统计
curl -H "Authorization: Bearer $ADMIN_KEY" http://127.0.0.1:8901/admin/stats

# API: 查看当前 manifest
curl -H "Authorization: Bearer $ADMIN_KEY" http://127.0.0.1:8901/admin/manifest

# API: 解绑机器（用户换机器后）
curl -X POST -H "Authorization: Bearer $ADMIN_KEY" \
     -H "Content-Type: application/json" \
     -d '{"key_prefix":"a1b2c3d4"}' \
     http://127.0.0.1:8901/admin/tokens/unbind

# API: 重置下载/带宽配额
curl -X POST -H "Authorization: Bearer $ADMIN_KEY" \
     -H "Content-Type: application/json" \
     -d '{"key_prefix":"a1b2c3d4"}' \
     http://127.0.0.1:8901/admin/tokens/reset-quota
```

## 文件说明

```
model-server/
├── auth_middleware.py    # 鉴权中间件（license + session + manifest + webhook）
├── nginx.conf           # nginx 配置（auth + rate limit）
├── deploy.sh            # 一键部署脚本
├── CHANGELOG.md         # 版本更新日志
├── README.md            # 本文件
└── TECHNICAL.md         # 详细技术文档
```

## 注意事项

- `MODEL_ADMIN_KEY` 是管理密钥，务必保密
- `MODEL_FILES_ROOT` 指向模型文件目录，middleware 会扫描生成 manifest（仅 vad/asr/punc 目录）
- License key 只在创建时显示一次，无法找回
- Session token 有效期 30 分钟，活跃下载自动续期
- `max_downloads` 计数的是"下载会话次数"，不是单个文件
- `max_machines` 控制一个 key 可绑定的机器数（默认 1）
- `webhook_url` 可选，下载完成时触发回调
- `auth.db` 是 SQLite 数据库，定期备份
- 日志在 `logs/` 目录，按天分割
- 客户端支持增量更新（再次运行只下载变更文件）和断点续传
