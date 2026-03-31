# 使用手册

## 概述

纯 ONNX 推理的中文语音识别，三阶段流水线：

```
Audio → [VAD] → Speech Segments → [ASR] → Raw Text → [Punc] → Punctuated Text
```

仅依赖 `onnxruntime` + `numpy`，无 PyTorch / FunASR。

## 快速开始

```bash
pip install onnxruntime numpy pyyaml soundfile
python scripts/download_models.py
python scripts/asr_tools.py transcribe audio.wav
```

## 命令行

```bash
# 纯文本（带标点）
python scripts/asr_tools.py transcribe audio.wav

# JSON（含时间戳）
python scripts/asr_tools.py transcribe audio.wav --json

# 批量
python scripts/asr_tools.py batch /path/to/audio_dir --output results.json

# WebSocket 服务
python scripts/asr_tools.py server --host 0.0.0.0 --port 2701
```

## Python API

```python
import sys
sys.path.insert(0, "<skill_dir>/scripts")
from asr_tools import ASRPipeline, load_config

config = load_config()
pipeline = ASRPipeline(config)
result = pipeline.transcribe("audio.wav")
print(result["text"])
```

### 单独使用各模块

```python
from vad_onnx import SileroVAD
from paraformer_onnx import ParaformerONNX
from punc_onnx import PunctuationONNX

vad = SileroVAD("models/vad/vad.onnx")
segments = vad.detect(audio_array)

asr = ParaformerONNX("models/asr")
text = asr.recognize(audio_segment)

punc = PunctuationONNX("models/punc")
result = punc.punctuate(raw_text)
```

## 跨平台

Windows / macOS / Linux 均可直接运行，依赖相同。Docker 部署：

```dockerfile
FROM python:3.11-slim
RUN pip install onnxruntime numpy pyyaml soundfile websockets
COPY . /app
WORKDIR /app
RUN python scripts/download_models.py
CMD ["python", "scripts/asr_server.py"]
```

## 常见问题

- GPU 加速：`pip install onnxruntime-gpu` 替代 `onnxruntime`
- 识别不准：确认 16kHz 采样率，检查 VAD 阈值，确认 am.mvn 已下载
- 无标点：确认 punc 模型目录存在且含 model.onnx
