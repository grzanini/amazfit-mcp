"""Transcreve um áudio (ogg/opus do Telegram, m4a, mp3, wav) localmente com faster-whisper. Uso: uv run --project tools/whisper tools/transcrever.py <arquivo> [modelo]"""
import sys, time
from faster_whisper import WhisperModel
path = sys.argv[1]; size = sys.argv[2] if len(sys.argv) > 2 else "small"
t0 = time.time()
model = WhisperModel(size, device="cpu", compute_type="int8")
segments, info = model.transcribe(path, language="pt", beam_size=5, vad_filter=True)
text = " ".join(s.text.strip() for s in segments)
print(text)
print(f"\n[{size}, {info.duration:.0f}s de áudio, {time.time()-t0:.1f}s]", file=sys.stderr)
