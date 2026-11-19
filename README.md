<img width="1604" height="626" alt="image" src="https://github.com/user-attachments/assets/db52960c-7b71-4134-a035-e3a140be4017" />

 # whisper.cpp — Unified Launcher

`whisper.cmd` is a drag-and-drop Windows launcher for **whisper.cpp** (`data\whisper-cli.exe`) with optional **Ollama** subtitle/text translation. It replaces the previous faster-whisper-xxl based wrapper: diarization and vocal/background extraction were intentionally removed.

---

## Requirements

- **Windows 10/11 x64**
- **whisper-cli** — `data\whisper-cli.exe` plus its DLLs (CUDA build included in `data\`). CUDA is used automatically when available; the CPU-only backends are included as well.
- **GGML models** — `models\ggml-large-v2.bin` (default transcription model) and `models\ggml-tiny.bin`. If one of them is missing, menu `[6]` Download Models calls `models\download-ggml-model.cmd <model>` and downloads **only that model** (BITS transfer from Hugging Face, internet required).
- **Silero VAD model** — `models\ggml-silero-v6.2.0.bin` (auto-downloaded on first use via `models\download-vad-model.cmd silero-v6.2.0`). Voice activity detection is enabled by default to reduce hallucinations on silence/music.
- **MDX vocal isolation** — `models\Kim_Vocal_2.onnx` plus the Python package `data\onnxruntime\` (numpy + onnxruntime; Python version must match the copied wheel). Used automatically before whisper-cli; if the model is missing it is downloaded via `models\download-mdx-model.cmd` (Hugging Face). Disable with `set FWHISPER_MDX=0`.
- **ffmpeg** — `data\ffmpeg.exe` (included). whisper-cli only reads `wav/mp3/flac/ogg`; video/other audio files are extracted to 16 kHz mono WAV automatically.
- **Python 3.7 or newer (3.10+ recommended)** with `requests` — only for the translation features. A working Python 3.7+ is verified; `pip install requests` runs automatically if needed.
- **Ollama** — only for translation. Installed automatically if missing; missing models are pulled automatically (`mistral-nemo`, or `translategemma:12b` for context option `B`).

Directory layout:

| File | Purpose |
|---|---|
| `whisper.cmd` | Drag-and-drop launcher |
| `helper.py` | Colored ASCII banner + progress runner |
| `mdx.py` | MDX Kim_Vocal_2 vocal isolation |
| `translate.py` | Ollama translation engine |
| `data\whisper-cli.exe` | whisper.cpp CLI (+ DLLs / ggml backends) |
| `data\ffmpeg.exe` | Audio extraction / conversion |
| `data\onnxruntime\` | Python runtime for MDX |
| `models\Kim_Vocal_2.onnx` | MDX vocal model |
| `models\ggml-large-v2.bin`, `ggml-tiny.bin`, ... | GGML Whisper models |
| `models\ggml-silero-v6.2.0.bin` | Silero VAD model |
| `models\download-ggml-model.cmd` | Download one GGML model |
| `models\download-vad-model.cmd` | Download Silero VAD model |
| `models\download-mdx-model.cmd` | Download MDX Kim_Vocal_2 model |

---

## Modes

| Key | Mode | Output |
|---|---|---|
| `[1]` | Transcribe Auto (language auto-detect) | `<name>.<LANG>.srt` (detected language uppercase, e.g. `.DE.srt`, `.EN.srt`, `.FR.srt`) |
| `[2]` | Transcribe German audio (language forced) | `<name>.DE.srt` |
| `[3]` | Transcribe English audio (language forced) | `<name>.EN.srt` |
| `[4]` | German audio → English: Whisper DE → Ollama DE→EN | `<name>.DE.srt` + `<name>.EN.srt` |
| `[5]` | English audio → German: Whisper EN → Ollama EN→DE | `<name>.EN.srt` + `<name>.DE.srt` |
| `[6]` | Download Models | shown when `ggml-tiny.bin`, `ggml-large-v2.bin`, the VAD model or `Kim_Vocal_2.onnx` is missing |

Transcription is performed by one `whisper-cli.exe` call for all files (model loaded once). Audio is extracted per file with ffmpeg, transcribed with `-l auto|de|en -osrt -oj`, and the detected language is read from the JSON output to rename the SRT.

**Voice activity detection:** `whisper-cli` runs with `--vad -vm models\ggml-silero-v6.2.0.bin` (Silero VAD v6.2.0). The VAD is applied to the WAV extracted by ffmpeg, so silence/music segments are skipped before decoding - this reduces phantom text ("hallucinations") on non-speech audio. If the VAD model is missing, it is downloaded automatically; if the download fails, transcription continues without VAD.

**Progress:** `whisper-cli` supports `-pp/--print-progress` (percentages to stderr, default 5% steps). `helper.py run` starts whisper-cli, filters stderr down to the progress lines and shows each reported percentage on its own line. Short files with `--vad`/MDX may only report the final `100%`; longer files report multiple steps (e.g. `25%`, `49%`, `75%`, `100%`). MDX vocal isolation reports its own `0%`–`100%` progress from `mdx.py`.

**Subtitle line wrapping:** lines are wrapped inside one timestamp only. One line if it fits (about 45 characters), otherwise two balanced lines (about 50/50), and for very long blocks three lines with about 40/40/20 percent of the text in the first two/third line. This follows the common subtitle guidelines (max. two lines, break after punctuation / before conjunctions, no orphan words).

**Subtitle block size:** whisper-cli runs with `-ojf` (full JSON with token timestamps). For transcription-only modes, `translate.py --words-srt` builds display blocks directly from the token timestamps. For translation modes, `translate.py --sentence-srt` first builds sentence-sized blocks, the translation is done on those blocks, and `translate.py --pair-srt` then splits source and target into matching display blocks (max two wrapped lines) using the source word timestamps. VAD token timestamps are relative to the filtered speech timeline, so they are mapped back onto the original segment interval; block starts are additionally trimmed to the measured speech onset. Source and translated subtitles always share identical timestamps and block boundaries.
The trailing punctuation of every translated block is aligned with its source block (a source comma stays a comma; a source sentence end stays a sentence end; continuing blocks never get a false full stop). The translation prompt already asks the model to preserve the source punctuation; `align_block_punctuation()` remains as a deterministic fallback for model slips. This post-processing uses `translate.py --words-srt` / `--sentence-srt` / `--pair-srt` and is skipped when Python is not available.

---

## Existing subtitles are reused (modes 1–5)

- A file is skipped when the subtitle the mode needs already exists.
- Modes 4/5 reuse an existing `<name>.DE.srt` / `<name>.EN.srt` and only translate the missing counterpart with Ollama (both directions).
- Folder drops are processed recursively; existing sidecar subtitles are detected and skipped.
- `Ctrl+C` cancels the current run; temp WAV files are removed.

## SRT files dropped directly (no Whisper)

Dropping an `.srt` file skips the menu and translates it with Ollama:

- `.EN.srt` → `.DE.srt`
- `.DE.srt` → `.EN.srt`
- plain `.srt` → `.DE.srt`
- any other language suffix (e.g. `.FR.srt`) → `.DE.srt` and `.EN.srt` (whichever is missing)
- existing targets are skipped

## TXT files dropped directly (no Whisper)

Dropping `.txt` files (or a folder containing only `.txt` files) translates plain text:

- Direction is asked: `[1]` any → German (`<name>.DE.txt`), `[2]` any → English (`<name>.EN.txt`).
- The context menu is reused: the value is the number of **sentences** used as context — sentences before (already translated) and after (source) — not subtitle blocks.
- 2-sentence chunks, two-pass translation (draft + review); paragraphs are preserved; output is UTF-8 with CRLF.
- Input encodings: UTF-8 (with/without BOM), Windows-1252, Latin-1.
- Existing targets and inputs already ending in `.DE.txt`/`.EN.txt` are skipped; mixing TXT with media/SRT files in one drop is rejected.
- `Ctrl+C` saves a `.partial.txt` for the current file.

---

## Context menu (translation)

```
0    No context                     Fastest
3    Good compromise                Moderate
A    Full entity tracking (Mistral) Slow
B    Full entity tracking (Gemma)   Slowest
```

`0`/`3`/`A` use `mistral-nemo`; `B` uses `translategemma:12b` (~8.1 GB VRAM — if it does not fit on the GPU it is very slow). `Enter` = `B`.

## Translation error handling

- No silent fallback: a failed translation never writes a "final" target file.
- Request timeout: **300 s** (override with `FWHISPER_OLLAMA_TIMEOUT`), no automatic retries.
- A batch aborts on the first failed file; already translated chunks are saved as `.partial.srt` / `.partial.txt`; exit code is non-zero.

---

## Testing

```
whisper.cmd testEnglish.mp4 testGerman.mp4      [1]  → testEnglish.EN.srt, testGerman.DE.srt
whisper.cmd testGerman.mp4                      [4]  → testGerman.DE.srt + testGerman.EN.srt
whisper.cmd Beispiel.DE.srt                          → Beispiel.EN.srt
whisper.cmd Englisch1.txt                            → Englisch1.DE.txt
```

## Optional model download scripts

- `models\download-ggml-model.cmd <model> [models_path]` — downloads a single GGML Whisper model (targeted; used by menu `[6]`). Model names: `tiny`, `base`, `small`, `medium`, `large-v1`, `large-v2`, `large-v3`, `large-v3-turbo` plus quantized variants (`small-q5_1`, `large-v2-q5_0`, ...). Existing files are skipped.
- `models\download-vad-model.cmd <model> [models_path]` — downloads a **Silero VAD** model (`silero-v5.1.2` or `silero-v6.2.0`) as `ggml-silero-<model>.bin`. Used automatically by the wrapper for Silero VAD v6.2.0 (auto-download if missing).
- `models\download-mdx-model.cmd` — downloads the **MDX Kim_Vocal_2** model from Hugging Face (`Kim_Vocal_2.onnx`, ~63.7 MiB) into `models\`. Used automatically by the wrapper for vocal isolation; existing files are skipped.
