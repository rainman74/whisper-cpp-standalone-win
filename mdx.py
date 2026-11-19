"""MDX Kim_Vocal_2 vocal isolation (self-contained, numpy + onnxruntime from data\\)."""
import os
import sys
import wave

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
sys.path.insert(0, DATA)
if hasattr(os, "add_dll_directory"):
    try:
        os.add_dll_directory(DATA)
    except OSError:
        pass

import onnxruntime as ort

MODEL = os.path.join(BASE, "models", "Kim_Vocal_2.onnx")
SR = 44100
N_FFT = 6144
HOP = 1024
DIM_F = 3072
DIM_T = 256
SEG = 256
TRIM = N_FFT // 2
CHUNK = HOP * (SEG - 1)
GEN = CHUNK - 2 * TRIM
WIN = (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)


def read_wav(path):
    with wave.open(path, "rb") as w:
        if w.getframerate() != SR or w.getsampwidth() != 2:
            raise ValueError("expected 16-bit 44100 Hz WAV")
        ch = w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        data = data.reshape(-1, ch).T
        if ch == 1:
            data = np.vstack([data, data])
        return data[:2].copy()


def write_wav(path, data):
    data = np.clip(data, -1.0, 1.0)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((data.T * 32767.0).astype("<i2").tobytes())


def stft(x):
    pad = N_FFT // 2
    mode = "reflect" if x.shape[1] > pad else "constant"
    xp = np.pad(x, ((0, 0), (pad, pad)), mode=mode)
    n_frames = 1 + (xp.shape[1] - N_FFT) // HOP
    frames = np.lib.stride_tricks.as_strided(
        xp,
        shape=(2, n_frames, N_FFT),
        strides=(xp.strides[0], HOP * xp.strides[1], xp.strides[1]),
        writeable=False,
    )
    spec = np.fft.rfft(frames * WIN, n=N_FFT, axis=-1).transpose(0, 2, 1)[:, :DIM_F, :]
    return np.stack([spec[0].real, spec[0].imag, spec[1].real, spec[1].imag], axis=0)[None].astype(np.float32)


def istft(pred, length):
    s = pred[0].reshape(2, 2, DIM_F, -1)
    pad = np.zeros((2, 2, N_FFT // 2 + 1 - DIM_F, s.shape[3]), dtype=np.float32)
    full = np.concatenate([s, pad], axis=2)
    comp = full[:, 0] + 1j * full[:, 1]
    frames = np.fft.irfft(comp, n=N_FFT, axis=1).transpose(0, 2, 1)
    out_len = N_FFT + HOP * (frames.shape[1] - 1)
    out = np.zeros((2, out_len), dtype=np.float32)
    wsum = np.zeros(out_len, dtype=np.float32)
    w2 = WIN * WIN
    for i in range(frames.shape[1]):
        p = i * HOP
        out[:, p:p + N_FFT] += frames[:, i, :] * WIN
        wsum[p:p + N_FFT] += w2
    wsum[wsum < 1e-8] = 1.0
    out /= wsum
    return out[:, N_FFT // 2:N_FFT // 2 + length]


def main():
    src, dst = sys.argv[1], sys.argv[2]
    mix = read_wav(src)
    peak = float(np.abs(mix).max())
    if peak <= 0:
        raise ValueError("empty audio")
    mix = mix / peak
    pad = GEN - (mix.shape[1] % GEN)
    mixture = np.pad(mix, ((0, 0), (TRIM, pad + TRIM)), mode="constant")

    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    sess = ort.InferenceSession(MODEL, providers=providers)

    outs = []
    total = mixture.shape[1] - 2 * TRIM
    for start in range(0, total, GEN):
        chunk = mixture[:, start:start + CHUNK]
        if chunk.shape[1] < CHUNK:
            chunk = np.pad(chunk, ((0, 0), (0, CHUNK - chunk.shape[1])))
        pred = sess.run(None, {"input": stft(chunk)})[0]
        outs.append(istft(pred, CHUNK)[:, TRIM:CHUNK - TRIM])
    vocals = np.concatenate(outs, axis=1)[:, :mix.shape[1]] * peak
    write_wav(dst, vocals)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"MDX error: {exc}", file=sys.stderr)
        sys.exit(1)