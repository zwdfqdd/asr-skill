# 模型说明与下载

## 模型组成

本 skill 使用三阶段推理管线，每个阶段对应一个 ONNX 模型。
具体文件列表由服务端 manifest 动态提供，客户端无需手动管理。

| 阶段 | 功能 | 目录 |
|------|------|------|
| VAD | 语音活动检测 | models/vad/ |
| ASR | 语音识别 | models/asr/ |
| Punc | 标点恢复 | models/punc/ |

## 下载

需要有效的 license key。在 skill 目录下创建 `license.json`：

```json
{
    "license_key": "your_key_here",
    "endpoint": "https://your-server.example.com"
}
```

然后运行：

```bash
python scripts/download_models.py
```

下载流程：
1. 客户端向服务端发起下载会话，提交 license key + 机器指纹
2. 服务端验证身份，返回临时 session token + 文件清单（含 SHA-256 校验）
3. 客户端逐个下载文件，ONNX 模型在内存中加密后写入 `.enc` 文件
4. 下载完成后会话自动关闭

模型文件明文不会写入磁盘。

## 目录结构

```
models/
├── model.key            # 加密密钥（自动生成，勿删除）
├── vad/
│   └── *.onnx.enc       # 加密模型
├── asr/
│   ├── *.onnx.enc       # 加密模型
│   ├── tokens.json
│   └── am.mvn
└── punc/
    ├── *.onnx.enc       # 加密模型
    └── tokens.json
```

路径在 `assets/asr_config.yaml` 中配置，支持相对路径（相对于 skill 目录）和绝对路径。

## 加密说明

- 算法：AES-256-GCM
- 密钥派生：base_key + 机器指纹 → SHA-256
- 绑定：模型与下载时的机器绑定，换机器无法解密
- 运行时：内存解密 → ONNX Runtime 加载，明文不落盘
- 兼容：如无 `.enc` 文件，自动回退加载明文 `.onnx`
