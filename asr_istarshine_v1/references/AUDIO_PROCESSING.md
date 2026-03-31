# 音频处理指南

## 输入要求

- 格式：WAV (PCM)
- 采样率：16000 Hz
- 声道：单声道 (mono)
- 位深：16-bit

其他格式需要 ffmpeg 转换。

## ffmpeg 转换

```bash
# 通用转换
ffmpeg -i input.mp3 -ar 16000 -ac 1 -f wav output.wav

# 从视频提取
ffmpeg -i video.mp4 -vn -ar 16000 -ac 1 audio.wav

# 截取片段
ffmpeg -i input.wav -ss 00:01:00 -to 00:05:00 -ar 16000 -ac 1 clip.wav

# 批量转换
for f in *.mp3; do ffmpeg -i "$f" -ar 16000 -ac 1 "${f%.mp3}.wav"; done
```

## 长音频

VAD 会自动切分语音段，可以直接处理长音频。超长音频（>1h）建议预分段：

```bash
ffmpeg -i long.wav -f segment -segment_time 600 -ar 16000 -ac 1 chunk_%03d.wav
```

## 降噪

```bash
# sox
sox noisy.wav -n trim 0 0.5 noiseprof noise.prof
sox noisy.wav clean.wav noisered noise.prof 0.21

# ffmpeg 音量归一化
ffmpeg -i input.wav -af "loudnorm=I=-16:TP=-1.5:LRA=11" -ar 16000 -ac 1 output.wav
```

## Python 加载音频

本 skill 使用 `soundfile` 读取音频，也支持 `wave` 模块作为 fallback：

```python
import soundfile as sf
data, sr = sf.read("audio.wav", dtype="float32")
```
