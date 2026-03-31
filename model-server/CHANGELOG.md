# CHANGELOG

## v0.2.0 (2026-03-31)

### 新功能
- 外网下载支持：通过 `https://nas.istarshine.com` 反代到 model-server
- 多机器绑定：`max_machines` 参数，一个 key 可绑定多台机器
- Webhook 通知：下载完成时触发 `session.completed` 回调
- 增量更新：本地 `manifest.json` 对比 SHA-256，只下载变更文件
- 断点续传：客户端支持 Range 请求，中断后自动恢复
- Session 自动续期：每次文件验证成功自动延长 TTL

### 改进
- 下载失败自动重试（3 次，递增等待 3s/6s/9s）
- Windows 控制台中文输出乱码修复
- Verify 端点异常捕获，崩溃时记录日志而非断开连接
- Verify 支持从 URI query param 解析 session token（fallback）
- 数据库 schema 自动迁移（download_log + license_keys 新列）
- Manifest 白名单扫描（仅 vad/asr/punc 目录）
- nginx 限流放宽至 30r/m burst=30

### 修复
- `try_files` + `alias` 组合导致 nginx 403
- `download_log` 表缺少 `key_hash` 列导致 verify 崩溃
- `sites-enabled/default` 的 `default_server` 抢占 80 端口请求

## v0.1.0 (2026-03-31)

### 初始版本
- License key + session 双层鉴权
- 机器指纹绑定（Windows/Linux/macOS）
- AES-256-GCM 客户端加密，模型绑定机器
- nginx 反代 + auth_request 文件分发
- Admin API：创建/吊销/解绑/重置配额
- SHA-256 文件完整性校验
- Session 自动过期清理
- 按天分割日志
