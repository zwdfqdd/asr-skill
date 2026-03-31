# 性能优化

## 推理性能

### CPU 参考（4 核）

| 音频时长 | 推理时间 | RTF |
|----------|----------|-----|
| 10s | ~1.5s | 0.15 |
| 30s | ~4s | 0.13 |
| 60s | ~7s | 0.12 |

### 线程控制

```bash
export OMP_NUM_THREADS=4
python asr_tools.py transcribe audio.wav
```

代码中已设置 ONNX Runtime 线程数，可在各模块构造函数中调整。

## 内存占用

| 组件 | 内存 |
|------|------|
| VAD | ~2MB |
| ASR | ~250 |
| Punc | ~270MB |
| 总计 | ~550MB |

## 优化建议

- Pipeline 对象只创建一次，多次调用 `transcribe()`
- 批量处理时串行即可（模型已占 1GB+ 内存）
- 使用 `onnxruntime-gpu` 可大幅加速 Paraformer 推理
- VAD 阈值调高（0.6-0.8）可减少误检，加快处理