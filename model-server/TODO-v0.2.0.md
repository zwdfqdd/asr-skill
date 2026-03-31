# v0.2.0 开发计划

## 已知问题 (v0.1.0)

### P0 - 核心
- [x] 外网下载支持：通过 `https://nas.istarshine.com` 反代到 model-server (8080)，已验证通过
- [x] nginx `X-Session` header 传递正常，`$arg_session` 已验证通过，URI fallback 保留作为保险

### P1 - 体验
- [x] test_asr.py 中文输出乱码（Windows 控制台编码问题）— 已修复
- [x] download_models.py 失败重试机制（3 次重试，递增等待）— 已实现
- [x] session 续期：每次文件验证成功自动延长 TTL，活跃下载不会超时

### P2 - 增强
- [x] 下载断点续传（Range 请求支持）— 客户端自动 resume
- [ ] 多机器绑定（一个 key 允许 N 台机器）
- [ ] webhook 通知（下载完成/异常回调）
- [x] 增量更新（只下载变更的文件）— 本地 manifest.json 对比 SHA-256

## 外网架构

### 现状
```
外网用户 → https://nas.istarshine.com (:443/:80)
              → Tengine/nginx (已有服务，占用 80/443)

内网用户 → http://192.168.223.5:8080
              → model-server nginx (auth + 文件分发)
              → auth_middleware (port 8901)
```

### 方案
在已有的 80/443 nginx/Tengine 中添加 model-server 的 location 路由，
将 /api/session/*, /files/*, /admin/* 反代到 127.0.0.1:8080 (或直接到 8901)。

详见 TECHNICAL.md 更新。
