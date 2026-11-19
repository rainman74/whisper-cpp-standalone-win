import sys
import os
import json
import wave
import numpy as np
import re
import glob
import subprocess
import json
import wave
import numpy as np
import requests
import itertools
import textwrap
import time
from datetime import datetime
from difflib import SequenceMatcher

OLLAMA_API = "http://127.0.0.1:11434"
MODEL      = "mistral-nemo"
TARGET_LANG = "de"   # target language: 'de' (default) or 'en'

OLLAMA_TIMEOUT = int(os.environ.get("FWHISPER_OLLAMA_TIMEOUT", "300"))


class TranslationError(Exception):
    pass

# ---------------------------------------------------------------------------
#  Ollama Setup
# ---------------------------------------------------------------------------

def _server_running():
    try:
        requests.get(f"{OLLAMA_API}/api/tags", timeout=2)
        return True
    except:
        return False

def _start_server():
    print("[FAST-WHISPER] Starting Ollama server ...", flush=True)
    print()
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.Popen(["ollama", "serve"], **kwargs)
    for _ in range(20):
        time.sleep(1)
        if _server_running():
            return True
    print("[FAST-WHISPER] Ollama server did not respond in time.")
    return False

def _model_present():
    try:
        r = requests.get(f"{OLLAMA_API}/api/tags", timeout=5)
        models = r.json().get("models", [])
        base = MODEL.split(":")[0]
        for m in models:
            name = m.get("name", "")
            if name == MODEL or name.split(":")[0] == base:
                return True
        return False
    except Exception:
        return False

def _ensure_model():
    if _model_present():
        return True
    print(f"[FAST-WHISPER] Model [{MODEL}] not found - downloading ...", flush=True)
    try:
        rc = subprocess.run(["ollama", "pull", MODEL]).returncode
    except Exception as e:
        print(f"[FAST-WHISPER] Model download failed: {e}", flush=True)
        rc = 1
    if rc != 0 or not _model_present():
        print(f"[FAST-WHISPER] Model [{MODEL}] is not available - run: ollama pull {MODEL}", flush=True)
        return False
    print(f"[FAST-WHISPER] Model [{MODEL}] downloaded.", flush=True)
    return True

def _preload_model():
    # Evict first so GPU layer settings are applied fresh on reload
    try:
        requests.post(f"{OLLAMA_API}/api/generate", json={
            "model": MODEL, "prompt": "", "keep_alive": 0
        }, timeout=10)
    except:
        pass
    print(f"[FAST-WHISPER] Preloading [{MODEL}] ...", flush=True)
    print()
    try:
        requests.post(f"{OLLAMA_API}/api/generate", json={
            "model": MODEL,
            "prompt": "",
            "stream": False,
            "keep_alive": "999h",
            "options": {
                "num_ctx": 4096,
                "num_gpu": 99,
            }
        }, timeout=OLLAMA_TIMEOUT)
    except:
        pass

def stop_ollama():
    try:
        subprocess.run(["taskkill", "/f", "/im", "ollama.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except:
        pass

def setup_ollama():
    # Always restart so OLLAMA_FLASH_ATTENTION and OLLAMA_KV_CACHE_TYPE
    # (set by whisper.cmd) are inherited by the server process.
    print(flush=True)
    if _server_running():
        print("[FAST-WHISPER] Restarting Ollama to apply GPU memory settings ...", flush=True)
        stop_ollama()
        time.sleep(3)
    if not _start_server():
        sys.exit(1)
    if not _ensure_model():
        sys.exit(1)
    _preload_model()
    result = subprocess.run(["ollama", "ps"], capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout.strip(), flush=True)
    print(flush=True)

# ---------------------------------------------------------------------------
#  Uebersetzungslogik
# ---------------------------------------------------------------------------

def get_gap(ts_prev, ts_next):
    try:
        end_prev = ts_prev.split(' --> ')[1].strip()
        start_next = ts_next.split(' --> ')[0].strip()
        t1 = datetime.strptime(end_prev, '%H:%M:%S,%f')
        t2 = datetime.strptime(start_next, '%H:%M:%S,%f')
        return (t2 - t1).total_seconds()
    except:
        return 999

def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

def _has_untranslated(source, result):
    """True if result likely contains untranslated words from the source language."""
    if not source or not result:
        return False
    if TARGET_LANG == "de":
        word_re = r'[a-zA-Z]{5,}'
        proper_re = r'(?<=[,;.!?] )[A-Z][a-z]{2,}'
    else:
        word_re = r'[a-zA-ZäöüÄÖÜß]{5,}'
        proper_re = r'(?<=[,;.!?] )[A-ZÄÖÜ][a-zäöüß]{2,}'
    proper = {m.group().lower() for m in re.finditer(proper_re, source)}
    src = {w.lower() for w in re.findall(word_re, source)} - proper
    res = {w.lower() for w in re.findall(word_re, result)}
    return bool(src & res)

def _detect_register(context_blocks, ctx):
    """Returns 'Sie', 'du', or None if register is unclear."""
    if TARGET_LANG != "de":
        return None
    if not context_blocks:
        return None
    sie = 0
    du  = 0
    for _, de in context_blocks[-max(ctx, 5):]:
        sie += len(re.findall(r'\bSie\b', de))
        du  += len(re.findall(r'\b[Dd]u\b|\b[Dd]ich\b|\b[Dd]ir\b', de))
    if sie + du < 2:
        return None
    if sie > du * 1.5:
        return 'Sie'
    if du > sie * 1.5:
        return 'du'
    return None

ANALYSIS_CTX_MAX = 10  # max entity-tracking window (capped by ctx)

def _translate_minimal(text):
    """Aggressive fallback: minimal prompt when full prompt fails to translate."""
    src_name = "English" if TARGET_LANG == "de" else "German"
    trg_name = "German" if TARGET_LANG == "de" else "English"
    prompt = f"[INST] Translate this {src_name} text to {trg_name}. Keep the final punctuation of the source (comma stays comma, never add a period). Output ONLY the {trg_name} translation:\n{text} [/INST]"
    try:
        response = requests.post(f"{OLLAMA_API}/api/generate", json={
            "model": MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.3, "num_predict": 256,
                        "num_ctx": 4096, "num_gpu": 99}
        }, timeout=OLLAMA_TIMEOUT)
        result = response.json().get('response', '').strip()
        result = result.replace('"', '').replace('\u201e', '').replace('\u201c', '').strip()
        return result if result else None
    except:
        return None

def translate_text(text, context_blocks=None, ctx=3, force_variety=False, _allow_retry=True, markers=False, numbered=0):
    if not text.strip():
        return text

    # Analysis context: last N DE segments for entity/gender tracking (capped by ctx)
    analysis_ctx = min(ctx, ANALYSIS_CTX_MAX)
    analysis_str = ""
    if context_blocks and analysis_ctx > 0:
        for _, de in context_blocks[-analysis_ctx:]:
            analysis_str += f"  {de}\n"

    # Translation context: recent source/target pairs for style matching
    style_ctx = min(ctx, 10)
    src_label = "EN" if TARGET_LANG == "de" else "DE"
    trg_label = "DE" if TARGET_LANG == "de" else "EN"
    context_str = ""
    if context_blocks and style_ctx > 0:
        for src_seg, trg_seg in context_blocks[-style_ctx:]:
            context_str += f"  {src_label}: {src_seg}\n  {trg_label}: {trg_seg}\n"

    # Inject register rule when dominant form is clear
    register = _detect_register(context_blocks, ctx)
    if register == 'Sie':
        register_rule = "10. Use the formal 'Sie' address form throughout.\n"
    elif register == 'du':
        register_rule = "10. Use the informal 'du' address form throughout.\n"
    else:
        register_rule = ""
    if markers:
        marker_rule = (
            f"11. The source is one sentence with exactly {markers} numbered clause tags <1>...</1>. "
            f"Translate the whole sentence coherently, then return exactly {markers} tag pairs in the same order. "
            "Each tag must contain only the translation of its clause. Never merge, split, reorder, omit, add or renumber tags. "
            "Do not translate or explain the tags.\n"
        )
    elif numbered:
        marker_rule = (
            f"11. The source is one sentence split into exactly {numbered} numbered lines. "
            f"Translate the whole sentence coherently and return exactly {numbered} lines, "
            "each starting with the same number and a period, containing only the translation of that clause. "
            "Never merge, split, reorder, omit, add or renumber lines.\n"
        )
    else:
        marker_rule = ""

    # Only include context sections when context exists
    if context_blocks:
        context_header = (
            f"Entity reference (last {analysis_ctx} segments):\n{analysis_str}\n"
            f"Recent translations:\n{context_str}\n"
        )
    else:
        context_header = ""

    target_name = "German" if TARGET_LANG == "de" else "English"
    if TARGET_LANG == "de":
        rules = (
            "1. Use natural German syntax (Verbzweitstellung, Satzklammer) — never adopt source-language structure.\n"
            "2. Treat each segment as part of a chain — track subject gender, number, and status from prior context. Never translate in isolation.\n"
            "3. Determine noun gender first. Align ALL dependent forms (articles, pronouns, adjectives) in correct gender, case (Nom/Gen/Dat/Akk), and number. Never mix genders within a reference chain.\n"
            "4. Check logical consistency: speaker POV, temporal sequence, and factual references must be coherent across segments.\n"
            "5. Transfer idioms and metaphors to natural German equivalents — never translate figurative language literally.\n"
            "6. No anglicisms when German equivalents exist. No domain-specific jargon for everyday expressions. Use period-appropriate vocabulary.\n"
            "7. Translate EVERY word to German — no English word may remain. This includes interjections, honorifics, and standalone words before punctuation.\n"
            "8. Vary phrasing — avoid mechanical repetitions from source structure. Fragments stay fragments. Keep concise.\n"
            "9. Preserve the source block final punctuation exactly: a source comma stays a comma, a source colon/semicolon stays a colon/semicolon, a source sentence end stays a sentence end. Never turn a source comma into a period. If the source block has no final punctuation, do not add a period.\n"
        )
    else:
        rules = (
            "1. Use natural English syntax and idiom — never adopt source-language structure.\n"
            "2. Treat each segment as part of a chain — track subject, number, and status from prior context. Never translate in isolation.\n"
            "3. Keep tense, aspect and modality consistent across segments; align pronouns with their referents.\n"
            "4. Check logical consistency: speaker POV, temporal sequence, and factual references must be coherent across segments.\n"
            "5. Transfer idioms and metaphors to natural English equivalents — never translate figurative language literally.\n"
            "6. No Germanisms when English equivalents exist. No domain-specific jargon for everyday expressions. Use period-appropriate vocabulary.\n"
            "7. Translate EVERY word to English — no German word may remain. This includes interjections, honorifics, and standalone words before punctuation.\n"
            "8. Vary phrasing — avoid mechanical repetitions from source structure. Fragments stay fragments. Keep concise.\n"
            "9. Preserve the source block final punctuation exactly: a source comma stays a comma, a source colon/semicolon stays a colon/semicolon, a source sentence end stays a sentence end. Never turn a source comma into a period. If the source block has no final punctuation, do not add a period.\n"
        )
    prompt = (
        f"[INST] Role: Professional Subtitle Translator\n\n"
        f"{context_header}"
        f"Translate to {target_name}: {text}\n\n"
        f"Rules:\n"
        f"{rules}"
        f"{register_rule}"
        f"{marker_rule}"
        f"Output ONLY the {target_name} translation. NEVER add parenthesized hints, notes, analysis, checklists, reasoning, or meta-commentary. [/INST]"
    )
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "think": True,
            "num_ctx": 4096,
            "num_gpu": 99,
            "temperature": 0.1 if force_variety else 0.0,
            "num_predict": 256,
            "repeat_penalty": 1.1,
            "top_p": 0.5,
            "stop": ["[INST]", "[/INST]"],
        }
    }
    try:
        response = requests.post(f"{OLLAMA_API}/api/generate", json=payload, timeout=OLLAMA_TIMEOUT)
        result = response.json().get('response', '').strip()
        result = result.replace('"', '').replace('\u201e', '').replace('\u201c', '').strip()
        if markers or numbered:
            result = result.strip()
            if not result:
                raise TranslationError("model returned an empty translation")
            return result
        # Strip all parenthesized content FIRST (subtitles are speech, never contain parentheses)
        if result:
            result = re.sub(r'\s*\([^)]*\)', '', result).strip()
            result = result.replace('(', '').replace(')', '').strip()
        # Truncate if result is excessively long (model dumped analysis)
        if result and len(result) > len(text) * 3:
            result = result.split('\n')[0].strip()
        # Remove "English → German" leakage (model mimics context format)
        if ' → ' in result:
            result = result.split(' → ')[-1].strip()
        # Remove "Target: ... Translation: German" leakage
        m = re.search(r'(?i)translation\s*:\s*(.+)', result, re.DOTALL)
        if m:
            result = m.group(1).strip()
        # Preserve trailing punctuation from source
        src = text.strip()
        if src and result:
            if src.endswith('...') or src.endswith('…'):
                if not result.endswith(('...', '…')):
                    result = result.rstrip('.…') + '…'
            elif src[-1] in '.!?':
                if result[-1] not in '.!?…':
                    result += src[-1]
        # Deduplicate repeated sentences within result
        if result:
            sentences = re.split(r'(?<=[.!?])\s+', result)
            if len(sentences) >= 2:
                seen = [sentences[0]]
                for s in sentences[1:]:
                    if all(similarity(s, prev) < 0.7 for prev in seen):
                        seen.append(s)
                result = ' '.join(seen)
        # Collapse double punctuation (but preserve ... ellipsis)
        result = re.sub(r'\.{2}(?!\.)', '.', result)
        result = result.strip()
        # Retry if result contains untranslated words or is too similar to source
        needs_retry = (
            _has_untranslated(text, result)
            or similarity(text.strip(), result.strip()) > 0.7
        )
        if _allow_retry and result and needs_retry:
            retry = translate_text(text, context_blocks, ctx,
                                   force_variety=True, _allow_retry=False)
            if retry and similarity(text.strip(), retry.strip()) <= 0.7:
                result = retry
            else:
                # Aggressive fallback: minimal prompt without complex rules
                minimal = _translate_minimal(text)
                if minimal and similarity(text.strip(), minimal.strip()) <= 0.7:
                    result = minimal
        if not result:
            raise TranslationError("model returned an empty translation")
        return result
    except TranslationError:
        raise
    except Exception as e:
        raise TranslationError(f"request failed: {e}") from e

def split_translation(text):
    if not text: return "", ""
    delimiters = [". ", "! ", "? ", "; ", ", "]
    best_split = -1
    mid_point = len(text) // 2
    for d in delimiters:
        idx = text.find(d)
        while idx != -1:
            diff = abs((idx + len(d)) - mid_point)
            if diff < (len(text) * 0.4):
                best_split = idx + len(d)
            idx = text.find(d, idx + 1)
    if best_split != -1:
        return text[:best_split].strip(), text[best_split:].strip()
    words = text.split()
    mid = len(words) // 2
    return " ".join(words[:mid]), " ".join(words[mid:])

def cleanup_wavs(input_file):
    srt_dir = os.path.dirname(input_file)
    srt_stem = os.path.splitext(os.path.basename(input_file))[0]
    if srt_stem.upper().endswith(".EN"):
        srt_stem = srt_stem[:-3]
    for f in glob.glob(os.path.join(srt_dir, srt_stem + "*.wav")):
        try:
            os.remove(f)
        except:
            pass

def write_srt(path, header, translated_blocks):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(header)
        for i, (ts, text) in enumerate(translated_blocks, 1):
            ts_line = ts.strip().splitlines()[-1]
            body_lines = _wrap_lines(text)
            if len(body_lines) > 2:
                relaxed = _wrap_lines(text, max_line=55)
                if len(relaxed) <= 2:
                    body_lines = relaxed
            body = "\n".join(body_lines)
            f.write(f"{i}\n{ts_line}\n{body}\n\n")

FLUSH_EVERY = 20   # write progress to disk every N translated blocks

def align_block_punctuation(source_text, translated_text):
    """Match the trailing punctuation of the translation to the source block (comma stays comma, etc.)."""
    src = source_text.strip()
    tgt = translated_text.strip()
    if not src or not tgt:
        return translated_text

    def split_closers(text):
        m = re.search(r'["\u201d\u2019\'\)\]]*$', text)
        closers = m.group(0) if m else ""
        return text[:len(text) - len(closers)].rstrip(), closers

    sbase, sclose = split_closers(src)
    tbase, tclose = split_closers(tgt)
    if not sbase or not tbase:
        return translated_text

    slast = sbase[-1]
    tlast = tbase[-1]
    sentence_end = ".!?\u2026"
    continuation = ",;:"

    if slast in continuation:
        if tlast in sentence_end:
            tbase = tbase[:-1] + slast
        elif tlast in continuation:
            tbase = tbase[:-1] + slast
        else:
            tbase = tbase + slast
    elif slast in sentence_end:
        if tlast in continuation:
            tbase = tbase[:-1] + slast
        elif tlast in sentence_end:
            tbase = tbase[:-1] + slast
        else:
            tbase = tbase + slast
    else:
        # Source continues mid-sentence: never end the translation with a full stop
        if tlast in sentence_end:
            tbase = tbase[:-1].rstrip()

    return (tbase + tclose).strip()

def process_srt(input_file, output_file, ctx=3, quiet_finish=False):
    start_timer = time.time()
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()
    parts = re.split(r'(\d+\n\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3})', content)
    header = parts[0]
    blocks = []
    for i in range(1, len(parts), 2):
        blocks.append({"timestamp": parts[i], "text": parts[i+1].strip()})
    total_blocks = len(blocks)
    translated_blocks = []
    context_blocks = []
    skip_next = False
    last_flush = 0
    try:
        for i in range(total_blocks):
            print(f"\r[FAST-WHISPER] {i+1} of {total_blocks} subtitles translated", end="", flush=True)
            if skip_next:
                skip_next = False
                continue
            current_en = blocks[i]["text"]
            if i < len(blocks) - 1 and not re.search(r'[.!?]$', current_en):
                gap = get_gap(blocks[i]["timestamp"], blocks[i+1]["timestamp"])
                if gap < 0.3:
                    next_en = blocks[i+1]["text"]
                    combined_en = current_en + " " + next_en
                    full_translation = translate_text(combined_en, context_blocks, ctx)
                    part1, part2 = split_translation(full_translation)
                    part1 = align_block_punctuation(blocks[i]["text"], part1)
                    part2 = align_block_punctuation(blocks[i+1]["text"], part2)
                    translated_blocks.append((blocks[i]["timestamp"], part1))
                    translated_blocks.append((blocks[i+1]["timestamp"], part2))
                    context_blocks.append((current_en, part1))
                    context_blocks.append((next_en, part2))
                    skip_next = True
                    # Incremental flush
                    if len(translated_blocks) - last_flush >= FLUSH_EVERY:
                        write_srt(output_file, header, translated_blocks)
                        last_flush = len(translated_blocks)
                    continue
            translation = translate_text(current_en, context_blocks, ctx)
            translation = align_block_punctuation(current_en, translation)
            if translated_blocks and similarity(translation, translated_blocks[-1][1]) > 0.8:
                translation = translate_text(current_en, context_blocks, ctx, force_variety=True)
            translated_blocks.append((blocks[i]["timestamp"], translation))
            context_blocks.append((current_en, translation))
            # Incremental flush
            if len(translated_blocks) - last_flush >= FLUSH_EVERY:
                write_srt(output_file, header, translated_blocks)
                last_flush = len(translated_blocks)

    except KeyboardInterrupt:
        print(f"\n\n[FAST-WHISPER] Translation cancelled: {len(translated_blocks)} of {total_blocks} subtitles translated.", flush=True)
        if translated_blocks:
            write_srt(output_file, header, translated_blocks)
            print(f"[FAST-WHISPER] Partial translation saved to:\n[FAST-WHISPER] {output_file}", flush=True)
        else:
            print("[FAST-WHISPER] Nothing to save.", flush=True)
        cleanup_wavs(input_file)
        stop_ollama()
        print()
        sys.exit(0)

    except TranslationError as e:
        print(f"\n\n[FAST-WHISPER] Translation failed for {os.path.basename(input_file)}: {e}", flush=True)
        if os.path.exists(output_file):
            try:
                os.remove(output_file)
            except OSError:
                pass
        if translated_blocks:
            base, ext = os.path.splitext(output_file)
            partial = f"{base}.partial{ext}"
            write_srt(partial, header, translated_blocks)
            print(f"[FAST-WHISPER] Partial translation saved to:\n[FAST-WHISPER] {partial}", flush=True)
        else:
            print("[FAST-WHISPER] Nothing to save.", flush=True)
        cleanup_wavs(input_file)
        raise

    write_srt(output_file, header, translated_blocks)
    cleanup_wavs(input_file)
    end_timer = time.time()
    duration = end_timer - start_timer
    hours, rem = divmod(duration, 3600)
    minutes, seconds = divmod(rem, 60)
    if not quiet_finish:
        print(f"\n\n[FAST-WHISPER] Translation finished in: {int(hours)}:{int(minutes):02}:{seconds:06.3f}\n")

def process_batch(job_file, ctx=3, clause_mode=False):
    global TARGET_LANG
    start_timer = time.time()
    jobs = []
    with open(job_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = [p.strip().strip('"') for p in line.split('|')]
            if len(parts) >= 3 and parts[0] and parts[1] and parts[2]:
                jobs.append((parts[0], parts[1], parts[2].lower()))
    total = len(jobs)
    if total == 0:
        print("[FAST-WHISPER] No translation jobs.", flush=True)
        return
    for idx, (src, dst, target) in enumerate(jobs, 1):
        TARGET_LANG = target
        prefix = "" if idx == 1 else "\n"
        print(f"{prefix}[FAST-WHISPER] Translating ({idx} of {total}): {os.path.basename(src)}", flush=True)
        print(flush=True)
        if not os.path.exists(src):
            print(f"[FAST-WHISPER] Source subtitle missing: {os.path.basename(src)}", flush=True)
            continue
        json_path = None
        if clause_mode:
            base = re.sub(r"\.(EN|DE)\.srt$", "", src, flags=re.IGNORECASE)
            candidate = base + ".json"
            if os.path.exists(candidate):
                json_path = candidate
        try:
            if json_path:
                process_srt_clauses(src, dst, json_path, ctx)
            else:
                process_srt(src, dst, ctx, quiet_finish=True)
        except TranslationError as e:
            print(f"[FAST-WHISPER] Translation aborted: {e}", flush=True)
            print(f"[FAST-WHISPER] Batch aborted - {idx - 1} of {total} file(s) completed.", flush=True)
            raise
        except Exception as e:
            print(f"[FAST-WHISPER] Translation aborted: {e}", flush=True)
            print(f"[FAST-WHISPER] Batch aborted - {idx - 1} of {total} file(s) completed.", flush=True)
            raise TranslationError(str(e)) from e
        if os.path.exists(dst):
            created_prefix = "\n" if clause_mode else "\n\n"
            print(f"{created_prefix}[FAST-WHISPER] Translation created: {os.path.basename(dst)}", flush=True)
        else:
            print(f"\n\n[FAST-WHISPER] Translation failed - no output written: {os.path.basename(dst)}", flush=True)
    end_timer = time.time()
    duration = end_timer - start_timer
    hours, rem = divmod(duration, 3600)
    minutes, seconds = divmod(rem, 60)
    print(f"\n[FAST-WHISPER] All translations finished in: {int(hours)}:{int(minutes):02}:{seconds:06.3f}\n", flush=True)

# ---------------------------------------------------------------------------
#  SRT sentence splitter (in-place, proportional timestamps)
# ---------------------------------------------------------------------------

def _srt_time_to_ms(t):
    h, m, rest = t.split(":")
    s, ms = rest.replace(".", ",").split(",")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def _ms_to_srt_time(ms):
    ms = max(0, int(ms))
    h = ms // 3600000
    ms -= h * 3600000
    m = ms // 60000
    ms -= m * 60000
    s = ms // 1000
    ms -= s * 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _split_long_sentence(s, max_chars=80):
    """Split a long sentence at commas/semicolons/dashes; never tear a subordinate clause."""
    if len(s) <= max_chars:
        return [s]
    candidates = []
    for pat in (", ", "; ", ": ", " – ", " - "):
        idx = 0
        while True:
            i = s.find(pat, idx)
            if i < 0:
                break
            candidates.append(i + len(pat))
            idx = i + 1
    if not candidates:
        return [s]
    mid = len(s) // 2
    candidates.sort(key=lambda c: abs(c - mid))
    for pos in candidates:
        left, right = s[:pos].strip(), s[pos:].strip()
        if len(left) < 15 or len(right) < 15:
            continue
        return _split_long_sentence(left, max_chars) + _split_long_sentence(right, max_chars)
    return [s]


def _wrap_lines(text, max_line=45, max_lines=3):
    """Break one subtitle's text into balanced lines (2 lines 50/50, 3 lines 40/40/20)."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return [text]
    if len(text) <= max_line:
        return [text]
    words = text.split()
    n = len(words)
    if n < 2:
        return [text]
    total = len(text)

    def best_split(k, targets):
        best = None
        for combo in itertools.combinations(range(1, n), k - 1):
            bounds = [0] + list(combo) + [n]
            lines = [" ".join(words[bounds[i]:bounds[i + 1]]) for i in range(k)]
            if any(not line for line in lines):
                continue
            target_lens = [total * t for t in targets]
            score = sum((len(line) - tl) ** 2 for line, tl in zip(lines, target_lens))
            score += 2 * sum(max(0, len(line) - max_line) ** 2 for line in lines)
            if best is None or score < best[0]:
                best = (score, lines)
        return best[1] if best else None

    if total <= 2 * max_line:
        lines = best_split(2, (0.5, 0.5))
        if lines and max(len(line) for line in lines) <= max_line:
            return lines
    lines = best_split(3, (0.4, 0.4, 0.2))
    if lines:
        return lines
    lines = best_split(2, (0.5, 0.5))
    return lines if lines else [text]

def _trim_block_starts(blocks, audio_path):
    """Move block starts forward when the VAD segment contains leading silence."""
    try:
        with wave.open(audio_path, "rb") as w:
            if w.getframerate() != 16000 or w.getsampwidth() != 2:
                return blocks
            data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    except Exception:
        return blocks
    if data.size == 0:
        return blocks
    frame = 320  # 20 ms at 16 kHz
    n = data.size // frame
    if n < 2:
        return blocks
    rms = np.sqrt(np.mean(data[:n * frame].reshape(n, frame) ** 2, axis=1))
    noise = float(np.percentile(rms, 20))
    threshold = max(0.010, noise * 5.0)
    for b in blocks:
        start = b[0][1]
        end = b[-1][2]
        i0 = max(0, start // 20)
        i1 = min(n, max(i0 + 1, end // 20), i0 + 75)  # search at most 1.5 s
        for i in range(i0, i1):
            if rms[i] >= threshold:
                if i > i0:
                    b[0][1] = i * 20
                break
    return blocks


def _json_words(json_path):
    with open(json_path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    words = []
    for seg in data.get("transcription", []):
        raw = []
        for tok in seg.get("tokens", []):
            t = tok.get("text", "")
            if not t or t.startswith("["):
                continue
            off = tok.get("offsets", {})
            raw.append((t, int(off.get("from", 0)), int(off.get("to", off.get("from", 0)))))
        if not raw:
            continue
        tmin = min(r[1] for r in raw)
        tmax = max(r[2] for r in raw)
        sfrom = int(seg.get("offsets", {}).get("from", tmin))
        sto = int(seg.get("offsets", {}).get("to", tmax))
        span = tmax - tmin
        scale = (sto - sfrom) / span if span > 0 else 1.0
        for t, s, e in raw:
            start = sfrom + int(round((s - tmin) * scale))
            end = sfrom + int(round((e - tmin) * scale))
            start = max(sfrom, min(start, sto))
            end = max(start, min(end, sto))
            if t[:1].isspace():
                words.append([t, start, end])
            elif words:
                words[-1][0] += t
                words[-1][2] = max(words[-1][2], end)
            else:
                words.append([t, start, end])
    return words


def _parse_srt_blocks(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        content = f.read()
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = []
    for part in re.split(r'\n\s*\n', content.strip()):
        part = part.strip()
        if not part:
            continue
        lines = part.split("\n")
        if len(lines) < 2:
            continue
        m = re.search(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})', lines[1])
        if not m:
            continue
        start = _srt_time_to_ms(m.group(1).replace(".", ","))
        end = _srt_time_to_ms(m.group(2).replace(".", ","))
        text = re.sub(r"\s+", " ", " ".join(lines[2:])).strip()
        blocks.append((start, end, text))
    return blocks


def _write_srt_blocks(path, blocks, max_line=45):
    with open(path, "w", encoding="utf-8", newline="") as f:
        for i, (start, end, text) in enumerate(blocks, 1):
            body = "\r\n".join(_wrap_lines(text, max_line))
            f.write(f"{i}\r\n{_ms_to_srt_time(start)} --> {_ms_to_srt_time(end)}\r\n{body}\r\n\r\n")



_FUNCTION_WORDS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "eines", "einem", "einen",
    "zu", "zum", "zur", "mit", "von", "vom", "für", "auf", "in", "im", "am", "an", "bei", "aus",
    "durch", "um", "über", "unter", "vor", "nach", "zwischen", "und", "oder", "aber", "dass",
    "weil", "wenn", "als", "wie", "ist", "sind", "war", "waren", "hat", "habe", "haben", "wird",
    "werden", "würde", "wurde",
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "with", "by", "from", "as", "that",
    "and", "or", "but", "is", "are", "was", "were", "be", "been", "being", "has", "have", "had",
    "will", "would", "can", "could", "should", "this", "these", "those", "it", "its", "we",
    "you", "they", "he", "she", "i",
}


def _item_text(item):
    return item if isinstance(item, str) else item[0].strip()


def _slice_balanced(items, k, weight, avoid_after=None):
    n = len(items)
    if k <= 1 or n <= 1:
        return [items]
    k = min(k, n)
    prefix = [0]
    for item in items:
        prefix.append(prefix[-1] + max(1, weight(item)))
    total = prefix[-1]
    cuts = []
    for i in range(1, k):
        target = total * i / k
        lo = (cuts[-1] + 1) if cuts else 1
        hi = n - (k - i)

        def score(x):
            s = abs(prefix[x] - target)
            prev = _item_text(items[x - 1]).lower().strip(".,;:!?…\"'")
            if avoid_after and prev in avoid_after:
                s += 6
            if _item_text(items[x - 1]).rstrip()[-1:] in ".,;:!?":
                s -= 2
            return s

        j = min(range(lo, hi + 1), key=score)
        cuts.append(j)
    result = []
    prev = 0
    for c in cuts + [n]:
        result.append(items[prev:c])
        prev = c
    return result


def split_pair_srt(json_path, src_srt, dst_srt, max_line=45):
    """Translate-size sentence blocks -> paired display blocks (max 2 lines, same timestamps)."""
    words = _json_words(json_path)
    src_blocks = _parse_srt_blocks(src_srt)
    dst_blocks = _parse_srt_blocks(dst_srt)
    if len(src_blocks) != len(dst_blocks):
        raise ValueError(f"block count mismatch: {len(src_blocks)} != {len(dst_blocks)}")

    wi = 0
    out_src = []
    out_dst = []
    for (s0, s1, st), (d0, d1, dt) in zip(src_blocks, dst_blocks):
        sw = []
        while wi < len(words) and words[wi][2] <= s1 + 50:
            sw.append(words[wi])
            wi += 1
        if not sw:
            out_src.append((s0, s1, st))
            out_dst.append((s0, s1, dt))
            continue

        ttext = re.sub(r"\s+", " ", dt).strip()
        tw = ttext.split()
        if not tw:
            raise ValueError("empty translation block")

        best = None
        max_n = min(len(sw), len(tw), 10)
        for n in range(1, max_n + 1):
            sc = _slice_balanced(sw, n, lambda w: max(1, len(w[0].strip())), None)
            tc = _slice_balanced(tw, n, lambda w: len(w) + 1, _FUNCTION_WORDS)
            if len(tc) != len(sc):
                continue
            stexts = ["".join(w[0] for w in c).strip() for c in sc]
            ttexts = [" ".join(c) for c in tc]
            if any(not x for x in stexts) or any(not x for x in ttexts):
                continue
            if all(len(_wrap_lines(x, max_line)) <= 2 for x in stexts) and \
               all(len(_wrap_lines(x, max_line)) <= 2 for x in ttexts):
                best = (sc, stexts, ttexts)
                break

        if best is None:
            n = max(1, max_n)
            sc = _slice_balanced(sw, n, lambda w: max(1, len(w[0].strip())), None)
            tc = _slice_balanced(tw, n, lambda w: len(w) + 1, _FUNCTION_WORDS)
            stexts = ["".join(w[0] for w in c).strip() for c in sc]
            ttexts = [" ".join(c) for c in tc]
            best = (sc, stexts, ttexts)

        sc, stexts, ttexts = best
        starts = []
        ends = []
        for i, c in enumerate(sc):
            a = s0 if i == 0 else c[0][1]
            b = s1 if i == len(sc) - 1 else c[-1][2]
            a = max(s0, min(a, s1))
            b = max(a, min(b, s1))
            if i > 0 and a < ends[-1]:
                a = ends[-1]
            starts.append(a)
            ends.append(max(a, b))

        for i in range(len(sc)):
            a = starts[i]
            b = ends[i]
            if b <= a:
                b = a + 100
            out_src.append((a, b, stexts[i]))
            out_dst.append((a, b, ttexts[i]))

    _write_srt_blocks(src_srt, out_src, max_line)
    _write_srt_blocks(dst_srt, out_dst, max_line)
    return len(out_src)


def _clause_units(words, s0, s1):
    """Split source words at clause punctuation and long word boundaries."""
    raw = []
    cur = []

    def nchars(items):
        return sum(len(w[0].strip()) + 1 for w in items)

    for w in words:
        cur.append(w)
        token = w[0].strip()
        punct = token.endswith((",", ";", ":", "—", "–"))
        if (punct and len(cur) >= 2 and nchars(cur) >= 12) or nchars(cur) >= 110:
            raw.append(cur)
            cur = []
    if cur:
        raw.append(cur)

    merged = []
    pending = []
    for c in raw:
        c = pending + c
        pending = []
        if len(c) < 2 or nchars(c) < 12:
            if merged:
                merged[-1].extend(c)
            else:
                pending = c
        else:
            merged.append(c)
    if pending:
        if merged:
            merged[-1].extend(pending)
        else:
            merged = [pending]

    out = []
    prev_end = s0
    for i, c in enumerate(merged):
        a = s0 if i == 0 else c[0][1]
        b = s1 if i == len(merged) - 1 else c[-1][2]
        a = max(s0, min(a, s1))
        if i > 0 and a < prev_end:
            a = min(prev_end, s1)
        b = max(a, min(b, s1))
        if b <= a:
            b = min(s1, a + 200)
        if b <= a:
            b = a
        prev_end = b
        text = "".join(w[0] for w in c).strip()
        if text:
            out.append((a, b, text))
    if not out:
        text = " ".join(w[0].strip() for w in words).strip()
        out = [(s0, s1, text)]
    cleaned = []
    for u in out:
        if cleaned and (cleaned[-1][1] - cleaned[-1][0] < 400 or len(cleaned[-1][2]) < 15 or u[1] - u[0] < 400 or len(u[2]) < 15):
            p = cleaned[-1]
            cleaned[-1] = (p[0], max(p[1], u[1]), (p[2] + " " + u[2]).strip())
        else:
            cleaned.append(u)
    return cleaned


def _parse_marked_clauses(result, count):
    pairs = re.findall(r'<(\d+)>(.*?)</\1>', result, re.DOTALL | re.IGNORECASE)
    data = {}
    for k, v in pairs:
        try:
            idx = int(k)
        except ValueError:
            continue
        val = re.sub(r'\s+', ' ', v).strip()
        if val:
            data[idx] = val
    if len(data) != count or any(i not in data for i in range(1, count + 1)):
        return None
    return [data[i] for i in range(1, count + 1)]



def _parse_numbered_clauses(result, count):
    data = {}
    for line in result.replace("\r", "\n").split("\n"):
        m = re.match(r'^\s*(\d+)\s*[.)\-:]\s*(.+?)\s*$', line)
        if not m:
            continue
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        val = re.sub(r'\s+', ' ', m.group(2)).strip().strip("*_ ")
        if val:
            data[idx] = val
    if len(data) != count or any(i not in data for i in range(1, count + 1)):
        return None
    return [data[i] for i in range(1, count + 1)]
def _translate_marked_clauses(marked, clauses, context_blocks, ctx):
    if len(clauses) == 1:
        return [translate_text(clauses[0][2], context_blocks, ctx)]
    last_error = None
    numbered_input = "\n".join(f"{i+1}. {c[2]}" for i, c in enumerate(clauses))
    for attempt in (0, 1):
        try:
            raw = translate_text(numbered_input, context_blocks, ctx,
                                 force_variety=(attempt == 1), numbered=len(clauses))
            parsed = _parse_numbered_clauses(raw, len(clauses))
            if parsed:
                return parsed
        except TranslationError:
            pass
    for attempt in (0, 1):
        try:
            raw = translate_text(marked, context_blocks, ctx,
                                 force_variety=(attempt == 1), markers=len(clauses))
            parsed = _parse_marked_clauses(raw, len(clauses))
            if parsed:
                return parsed
        except TranslationError as exc:
            last_error = exc
    # Last resort: translate each clause in the sentence context.
    out = []
    for _, _, src_text in clauses:
        out.append(translate_text(src_text, context_blocks, ctx))
    if not out:
        raise TranslationError(f"could not translate marked clauses: {last_error}")
    return out


def process_srt_clauses(input_file, output_file, json_path, ctx=3):
    """Translate sentence blocks with clause markers, then write clause-level SRTs."""
    start_timer = time.time()
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()
    parts = re.split(r'(\d+\n\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3})', content)
    header = parts[0]
    blocks = []
    for i in range(1, len(parts), 2):
        blocks.append({"timestamp": parts[i], "text": parts[i + 1].strip()})

    words = _json_words(json_path)
    plans = []
    wi = 0
    for block in blocks:
        m = re.search(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', block["timestamp"])
        if not m:
            continue
        s0 = _srt_time_to_ms(m.group(1))
        s1 = _srt_time_to_ms(m.group(2))
        sw = []
        while wi < len(words) and words[wi][2] <= s1 + 50:
            sw.append(words[wi])
            wi += 1
        clauses = _clause_units(sw, s0, s1) if sw else [(s0, s1, block["text"])]
        plans.append((s0, s1, clauses))

    translated_blocks = []
    source_blocks = []
    context_blocks = []
    total = len(plans)
    last_flush = 0
    try:
        for idx, (s0, s1, clauses) in enumerate(plans):
            print(f"\r[FAST-WHISPER] {idx + 1} of {total} subtitles translated", end="", flush=True)
            marked = " ".join(f"<{i+1}>{c[2]}</{i+1}>" for i, c in enumerate(clauses))
            targets = _translate_marked_clauses(marked, clauses, context_blocks, ctx)
            if len(targets) != len(clauses):
                raise TranslationError("clause translation count mismatch")
            for j, (a, b, src_text) in enumerate(clauses):
                ts = f"{_ms_to_srt_time(a)} --> {_ms_to_srt_time(b)}"
                tgt = align_block_punctuation(src_text, targets[j])
                source_blocks.append((ts, src_text))
                translated_blocks.append((ts, tgt))
            src_sentence = " ".join(c[2] for c in clauses)
            tgt_sentence = " ".join(targets)
            context_blocks.append((src_sentence, tgt_sentence))
            if len(translated_blocks) - last_flush >= FLUSH_EVERY:
                write_srt(output_file, header, translated_blocks)
                last_flush = len(translated_blocks)
    except KeyboardInterrupt:
        print(f"\n\n[FAST-WHISPER] Translation cancelled: {len(translated_blocks)} of {total} sentences translated.", flush=True)
        if translated_blocks:
            write_srt(output_file, header, translated_blocks)
            print(f"[FAST-WHISPER] Partial translation saved to:\n[FAST-WHISPER] {output_file}", flush=True)
        else:
            print("[FAST-WHISPER] Nothing to save.", flush=True)
        cleanup_wavs(input_file)
        stop_ollama()
        print()
        sys.exit(0)
    except TranslationError as e:
        print(f"\n\n[FAST-WHISPER] Translation failed for {os.path.basename(input_file)}: {e}", flush=True)
        if os.path.exists(output_file):
            try:
                os.remove(output_file)
            except OSError:
                pass
        if translated_blocks:
            base, ext = os.path.splitext(output_file)
            partial = f"{base}.partial{ext}"
            write_srt(partial, header, translated_blocks)
            print(f"[FAST-WHISPER] Partial translation saved to:\n[FAST-WHISPER] {partial}", flush=True)
        else:
            print("[FAST-WHISPER] Nothing to save.", flush=True)
        cleanup_wavs(input_file)
        raise

    write_srt(output_file, header, translated_blocks)
    # Replace the sentence-level source SRT with the final clause-level blocks.
    write_srt(input_file, header, source_blocks)
    cleanup_wavs(input_file)
    duration = time.time() - start_timer
    hours, rem = divmod(duration, 3600)
    minutes, seconds = divmod(rem, 60)
    if total > 0:
        print(flush=True)
        print(f"[FAST-WHISPER] Clause translation finished in: {int(hours)}:{int(minutes):02}:{seconds:06.3f}", flush=True)


def split_srt_from_words(json_path, srt_path, audio_path=None, max_chars=60, max_line=45):
    """Build display subtitle blocks from whisper-cli full JSON token timestamps."""
    with open(json_path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    words = []  # [text_with_leading_space, start_ms, end_ms]
    for seg in data.get("transcription", []):
        raw = []
        for tok in seg.get("tokens", []):
            t = tok.get("text", "")
            if not t or t.startswith("["):
                continue
            off = tok.get("offsets", {})
            raw.append((t, int(off.get("from", 0)), int(off.get("to", off.get("from", 0)))))
        if not raw:
            continue

        # token timestamps refer to the VAD-filtered (concatenated) audio;
        # map them back to the original segment interval from the JSON.
        tmin = min(r[1] for r in raw)
        tmax = max(r[2] for r in raw)
        sfrom = int(seg.get("offsets", {}).get("from", tmin))
        sto = int(seg.get("offsets", {}).get("to", tmax))
        span = tmax - tmin
        scale = (sto - sfrom) / span if span > 0 else 1.0

        for t, s, e in raw:
            start = sfrom + int(round((s - tmin) * scale))
            end = sfrom + int(round((e - tmin) * scale))
            start = max(sfrom, min(start, sto))
            end = max(start, min(end, sto))
            if t[:1].isspace():
                words.append([t, start, end])
            elif words:
                words[-1][0] += t
                words[-1][2] = max(words[-1][2], end)
            else:
                words.append([t, start, end])

    def wtext(ws):
        return "".join(w[0] for w in ws).strip()

    blocks = []
    cur = []
    for w in words:
        cur.append(w)
        text = wtext(cur)
        if not text:
            continue
        if text[-1] in ".!?\u2026" and len(text) >= 25:
            blocks.append(cur)
            cur = []
        elif len(text) >= max_chars:
            cut = None
            for j in range(len(cur) - 1, 0, -1):
                if wtext(cur[:j])[-1:] in ",;:":
                    cut = j
                    break
            if cut:
                blocks.append(cur[:cut])
                cur = cur[cut:]
            else:
                blocks.append(cur)
                cur = []
    if cur:
        blocks.append(cur)

    # Merge very short blocks into the next one (unless it is a single block)
    merged = []
    for b in blocks:
        if b:
            merged.append(b)
    blocks = []
    for b in merged:
        if blocks and len(wtext(blocks[-1])) < 15:
            blocks[-1] = blocks[-1] + b
        else:
            blocks.append(b)

    if audio_path and os.path.exists(audio_path):
        blocks = _trim_block_starts(blocks, audio_path)

    def fmt(ms):
        ms = max(0, int(ms))
        h = ms // 3600000; ms -= h * 3600000
        m = ms // 60000; ms -= m * 60000
        s = ms // 1000; ms -= s * 1000
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(srt_path, "w", encoding="utf-8", newline="") as f:
        for i, b in enumerate(blocks, 1):
            start = b[0][1]
            end = b[-1][2]
            if end <= start:
                end = start + 300
            text = wtext(b)
            body = "\r\n".join(_wrap_lines(text, max_line))
            f.write(f"{i}\r\n{fmt(start)} --> {fmt(end)}\r\n{body}\r\n\r\n")
    return len(blocks)



def split_srt_file(path, max_chars=0):
    """Split SRT blocks that contain several sentences into one block per sentence."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        content = f.read()
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    parts = [p.strip() for p in re.split(r'\n\s*\n', content.strip()) if p.strip()]
    raw_blocks = []
    for part in parts:
        lines = part.split("\n")
        if len(lines) < 2:
            continue
        times = lines[1]
        m = re.search(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})', times)
        if not m:
            continue
        start = _srt_time_to_ms(m.group(1).replace(".", ","))
        end = _srt_time_to_ms(m.group(2).replace(".", ","))
        text = re.sub(r"\s+", " ", " ".join(lines[2:])).strip()
        raw_blocks.append((start, end, text))
    # Merge blocks that end mid-sentence (whisper sometimes splits sentences across segments)
    merged = []
    for start, end, text in raw_blocks:
        if merged and not re.search(r'[.!?…]["”’)]*\s*$', merged[-1][2]):
            prev = merged.pop()
            merged.append((prev[0], end, (prev[2] + " " + text).strip()))
        else:
            merged.append((start, end, text))
    out_blocks = []
    for start, end, text in merged:
        sentences = [s.strip() for s in split_sentences_txt(text) if s.strip()]
        pieces = []
        for s in sentences:
            if max_chars > 0:
                pieces.extend(_split_long_sentence(s, max_chars))
            else:
                pieces.append(s)
        if len(pieces) <= 1:
            out_blocks.append((start, end, text))
            continue
        total = max(1, end - start)
        weights = [max(1, len(x)) for x in pieces]
        wsum = sum(weights)
        cur = start
        for i, s in enumerate(pieces):
            if i == len(pieces) - 1:
                seg_end = end
            else:
                seg_end = cur + int(round(total * weights[i] / wsum))
                seg_end = max(cur + 300, min(seg_end, end - 300 * (len(pieces) - i - 1)))
            out_blocks.append((cur, seg_end, s))
            cur = seg_end
    with open(path, "w", encoding="utf-8", newline="") as f:
        for i, (start, end, text) in enumerate(out_blocks, 1):
            body = "\r\n".join(_wrap_lines(text))
            f.write(f"{i}\r\n{_ms_to_srt_time(start)} --> {_ms_to_srt_time(end)}\r\n{body}\r\n\r\n")
    return len(out_blocks)

# ---------------------------------------------------------------------------
#  TXT translation (plain text: sentences / paragraphs, no timestamps)
# ---------------------------------------------------------------------------

TXT_ABBREVIATIONS_EN = [
    "Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "Sr.", "Jr.", "St.", "vs.",
    "etc.", "e.g.", "i.e.", "Inc.", "Ltd.", "Corp.", "No.", "Vol.",
    "Fig.", "Rev.", "Gen.", "Sgt.", "Capt.", "Lt.", "Col.", "Maj.",
    "approx.", "dept.", "est.", "govt.", "Jan.", "Feb.", "Mar.",
    "Apr.", "Jun.", "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec.",
]
TXT_ABBREVIATIONS_DE = [
    "Dr.", "Prof.", "Hr.", "Fr.", "Nr.", "Str.", "ca.", "z.B.",
    "d.h.", "u.a.", "v.a.", "bzgl.", "bzw.", "evtl.", "ggf.",
    "inkl.", "usw.", "etc.", "Abs.", "Abt.", "Bd.", "Jh.",
]
TXT_PLACEHOLDER = "\x00"

TXT_NEUTER_NOUNS = [
    'Stück', 'Mädchen', 'Kind', 'Haus', 'Ende', 'Mal', 'Buch', 'Bild',
    'Fest', 'Tier', 'Spiel', 'Ziel', 'Beispiel', 'Problem', 'Ergebnis',
    'Zeichen', 'System', 'Thema', 'Jahr', 'Land', 'Wort', 'Auto', 'Geld',
    'Recht', 'Teil', 'Mittel', 'Paar', 'Gebäude', 'Unternehmen',
    'Ereignis', 'Verhältnis', 'Verfahren', 'Element',
]
TXT_SUBST_ADJECTIVES = [
    'Brave', 'Unartige', 'Gute', 'Böse', 'Kleine', 'Große', 'Alte',
    'Junge', 'Kranke', 'Gesunde', 'Arme', 'Reiche', 'Fremde', 'Bekannte',
    'Verwandte', 'Angestellte', 'Beamte', 'Jugendliche', 'Erwachsene',
    'Deutsche', 'Angehörige', 'Abgeordnete', 'Verletzte', 'Verstorbene',
    'Verdächtige', 'Angeklagte', 'Vorsitzende',
]
TXT_COLORS_DE = ['rot', 'blau', 'grün', 'schwarz', 'weiß', 'gelb',
                 'braun', 'grau', 'gold', 'silber', 'lila', 'rosa', 'orange']
TXT_CLOTHING_STEMS = ['jacke', 'jacken', 'mantel', 'mäntel', 'hemd',
                      'hemden', 'hose', 'hosen', 'kittel', 'weste',
                      'westen', 'rock', 'kleid', 'anzug', 'coat',
                      'jacket', 'cape']
TXT_SUBORD_CONJ = ['dass', 'weil', 'obwohl', 'wenn', 'obgleich', 'sodass',
                   'sobald', 'solange', 'nachdem', 'bevor', 'damit',
                   'indem', 'sofern', 'falls', 'seitdem', 'ehe']
TXT_MONTHS_DE = ('Januar|Februar|März|April|Mai|Juni|Juli|'
                 'August|September|Oktober|November|Dezember')
TXT_EN_LEAK_FIXES = {
    'completely': 'völlig', 'however': 'jedoch', 'therefore': 'daher',
    'furthermore': 'darüber hinaus', 'meanwhile': 'inzwischen',
    'actually': 'tatsächlich', 'basically': 'grundsätzlich',
    'essentially': 'im Wesentlichen', 'particularly': 'besonders',
    'approximately': 'ungefähr', 'unfortunately': 'leider',
    'immediately': 'sofort', 'obviously': 'offensichtlich',
    'currently': 'derzeit', 'recently': 'kürzlich', 'previously': 'zuvor',
    'additionally': 'zusätzlich', 'especially': 'besonders',
    'significantly': 'erheblich', 'ultimately': 'letztendlich',
    'apparently': 'offenbar', 'frequently': 'häufig',
    'occasionally': 'gelegentlich', 'generally': 'im Allgemeinen',
    'entirely': 'vollständig', 'merely': 'lediglich', 'slightly': 'leicht',
    'largely': 'weitgehend', 'potentially': 'möglicherweise',
    'relatively': 'relativ', 'increasingly': 'zunehmend',
    'primarily': 'hauptsächlich',
}


def _txt_protect_abbreviations(text):
    abbrs = sorted(TXT_ABBREVIATIONS_DE + TXT_ABBREVIATIONS_EN,
                   key=len, reverse=True)
    for abbr in abbrs:
        text = text.replace(abbr, abbr.replace(".", TXT_PLACEHOLDER))
    text = text.replace("...", TXT_PLACEHOLDER * 3)
    return text


def _txt_restore_abbreviations(text):
    return text.replace(TXT_PLACEHOLDER, ".")


def split_sentences_txt(text):
    protected = _txt_protect_abbreviations(text)
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z\u00C0-\u00DC"\'\u201E\u201C\u00AB(])', protected)
    return [_txt_restore_abbreviations(p).strip() for p in parts if p.strip()]


def build_txt_chunks(text, sentences_per_chunk=2):
    paragraphs = re.split(r'\n\s*\n', text.strip())
    chunks = []
    para_indices = []
    for pi, para in enumerate(paragraphs):
        para = para.strip()
        if not para:
            continue
        sentences = split_sentences_txt(para)
        for i in range(0, len(sentences), sentences_per_chunk):
            chunks.append(" ".join(sentences[i:i + sentences_per_chunk]))
            para_indices.append(pi)
    return chunks, para_indices


def _txt_fix_german(text):
    for en, de in TXT_EN_LEAK_FIXES.items():
        text = re.sub(rf'\b{en}\b', de, text, flags=re.IGNORECASE)
    for noun in TXT_NEUTER_NOUNS:
        text = re.sub(rf'\beine\b(\s+{re.escape(noun)})\b', r'ein\1', text)
        text = re.sub(rf'\bEine\b(\s+{re.escape(noun)})\b', r'Ein\1', text)
    for adj in TXT_SUBST_ADJECTIVES:
        text = re.sub(rf'\bder {re.escape(adj)}\b', f'der {adj}n', text)
    for color in TXT_COLORS_DE:
        for stem in TXT_CLOTHING_STEMS:
            text = re.sub(rf'\b{color}{stem}\w+', f'{color} gekleidet',
                          text, flags=re.IGNORECASE)
    text = re.sub(rf'auf de[mn] (\d+\.\s*(?:{TXT_MONTHS_DE}))', r'am \1', text)
    for conj in TXT_SUBORD_CONJ:
        text = re.sub(rf'(\w) ({conj})\b', rf'\1, \2', text)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'\b(dass|und|oder|aber|auch|noch|schon|dann|denn)\s+\1\b',
                  r'\1', text, flags=re.IGNORECASE)
    text = re.sub(r'\.\.(?!\.)', '.', text)
    return text


def _txt_postprocess(text, target):
    text = re.sub(
        r'\n\s*(?:'
        r'Corrected |Corrections?\s|Changes?\s|Notes?\s*:|Explanation|Revision'
        r'|Korrektur|Änderungen|Übersetzung\s*[:(]|Translation\s'
        r'|Original\s*[:(]|\(Translation\b|\(Korrektur\b|\(Übersetzung\b'
        r').*', '', text, flags=re.DOTALL | re.IGNORECASE)
    if len(text) > 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    for ch in ['\u201e', '\u201c', '\u201d']:
        text = text.replace(ch, '')
    text = re.sub(r'  +', ' ', text)
    if target == "de":
        text = _txt_fix_german(text)
    return text.strip()


TXT_SYSTEM_TRANSLATE = (
    "You are a professional translator. Translate the text into {tgt}.\n"
    "The source language may be English or German - detect it automatically.\n"
    "- Produce natural, fluent {tgt} with correct grammar, gender, and case.\n"
    "- Use natural {tgt} sentence structure. Do not mirror the source syntax.\n"
    "- Use exclusively established {tgt} vocabulary. Never invent compound words.\n"
    "  If unsure, use a descriptive phrase instead.\n"
    "- Translate ALL terms completely. No source-language words may remain.\n"
    "- Preserve punctuation, parentheses, and dashes exactly.\n"
    "- Output ONLY the translation. No notes, explanations, headings, or labels."
)

TXT_SYSTEM_TRANSLATE_DE = (
    "\n- Use natural German syntax (Verbzweitstellung, Satzklammer).\n"
    "- Translate EVERY word to German - no English word may remain.\n"
    "- Check logical consistency and grammatical gender/case across sentences."
)

TXT_SYSTEM_TRANSLATE_EN = (
    "\n- Use natural English syntax and idiom.\n"
    "- Translate EVERY word to English - no German word may remain.\n"
    "- Check tense, aspect and pronouns for consistency across sentences."
)

TXT_SYSTEM_REVIEW = (
    "You are a {tgt} translation proofreader.\n"
    "Correct the draft translation based on the original. You may restructure freely.\n\n"
    "- Terminological consistency: replace ALL remaining source-language terms with {tgt} equivalents.\n"
    "- Grammatical precision: fix gender agreement, case errors, and verb conjugation.\n"
    "- Vocabulary: replace invented compound words with real {tgt} words or descriptive phrases.\n"
    "- Idiomatic correctness: replace word-for-word translations with natural {tgt} phrasing.\n"
    "- Structural integrity: do not add or omit information. Match the original precisely.\n"
    "- CRITICAL: Output ONLY the corrected {tgt} text.\n"
    "  Do NOT output change notes, explanations, or 'Corrected' sections."
)


def _txt_call(system_prompt, user_msg, temperature=0.05):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "options": {
            "num_ctx": 4096,
            "num_gpu": 99,
            "temperature": temperature,
            "num_predict": 768,
            "repeat_penalty": 1.1,
            "top_p": 0.7,
        }
    }
    response = requests.post(f"{OLLAMA_API}/api/chat", json=payload, timeout=OLLAMA_TIMEOUT)
    return response.json().get("message", {}).get("content", "").strip()


def translate_txt_chunk(text, target, prev_sentences, next_sentences):
    if not text.strip():
        return text
    src_name = "German" if target == "en" else "English"
    tgt_name = "German" if target == "de" else "English"
    system = TXT_SYSTEM_TRANSLATE.format(tgt=tgt_name)
    system += TXT_SYSTEM_TRANSLATE_DE if target == "de" else TXT_SYSTEM_TRANSLATE_EN
    user_msg = ""
    if prev_sentences:
        user_msg += "Previous translated sentences (for consistency):\n"
        user_msg += "\n".join(prev_sentences) + "\n\n"
    if next_sentences:
        user_msg += "Next source sentences (for context only, do not translate):\n"
        user_msg += " ".join(next_sentences) + "\n\n"
    user_msg += f"Translate:\n{text}"
    try:
        draft = _txt_call(system, user_msg, temperature=0.05)
        draft = _txt_postprocess(draft, target)
        if not draft:
            raise TranslationError("model returned an empty translation")
        review_system = TXT_SYSTEM_REVIEW.format(tgt=tgt_name)
        review_msg = f"Original:\n{text}\n\nTranslation ({tgt_name}):\n{draft}"
        corrected = _txt_call(review_system, review_msg, temperature=0.0)
        corrected = _txt_postprocess(corrected, target)
        return corrected if corrected else draft
    except TranslationError:
        raise
    except Exception as e:
        raise TranslationError(f"request failed: {e}") from e


def _read_txt_file(path):
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="latin-1") as f:
        return f.read()


def _write_txt_output(path, translated_chunks, para_indices):
    with open(path, "w", encoding="utf-8", newline="") as f:
        prev_para = None
        for i, chunk in enumerate(translated_chunks):
            if prev_para is not None and para_indices[i] != prev_para:
                f.write("\r\n\r\n")
            elif prev_para is not None:
                f.write(" ")
            f.write(chunk)
            prev_para = para_indices[i]
        f.write("\r\n")


def _txt_partial_path(dst):
    base, _ext = os.path.splitext(dst)
    return base + ".partial.txt"


def process_txt_file(src, dst, ctx=3, target="de"):
    start_timer = time.time()
    content = _read_txt_file(src)
    chunks, para_indices = build_txt_chunks(content)
    total = len(chunks)
    if total == 0:
        print(f"[FAST-WHISPER] No text found in {os.path.basename(src)} - skipped.", flush=True)
        return False

    translated = []
    prev_translated_sentences = []
    all_src_sentences = []
    for chunk in chunks:
        all_src_sentences.append(split_sentences_txt(chunk))

    try:
        for i, chunk in enumerate(chunks):
            print(f"\r[FAST-WHISPER] Translating {i + 1} of {total} chunks ...  ", end="", flush=True)
            next_sentences = []
            for j in range(i + 1, len(chunks)):
                next_sentences.extend(all_src_sentences[j])
                if len(next_sentences) >= ctx:
                    break
            translation = translate_txt_chunk(
                chunk, target,
                prev_translated_sentences[-ctx:] if ctx > 0 else [],
                next_sentences[:ctx] if ctx > 0 else [])

            if translated and similarity(translation, translated[-1]) > 0.8:
                translation = translate_txt_chunk(
                    chunk, target,
                    prev_translated_sentences[-ctx:] if ctx > 0 else [],
                    next_sentences[:ctx] if ctx > 0 else [])

            translated.append(translation)
            prev_translated_sentences.extend(split_sentences_txt(translation))

    except KeyboardInterrupt:
        print(f"\n\n[FAST-WHISPER] Translation cancelled: {len(translated)} of {total} chunks translated.", flush=True)
        if translated:
            partial = _txt_partial_path(dst)
            _write_txt_output(partial, translated, para_indices)
            print(f"[FAST-WHISPER] Partial translation saved to:\n[FAST-WHISPER] {partial}", flush=True)
        else:
            print("[FAST-WHISPER] Nothing to save.", flush=True)
        stop_ollama()
        sys.exit(0)

    except TranslationError as e:
        print(f"\n\n[FAST-WHISPER] Translation failed for {os.path.basename(src)}: {e}", flush=True)
        if os.path.exists(dst):
            try:
                os.remove(dst)
            except OSError:
                pass
        if translated:
            partial = _txt_partial_path(dst)
            _write_txt_output(partial, translated, para_indices)
            print(f"[FAST-WHISPER] Partial translation saved to:\n[FAST-WHISPER] {partial}", flush=True)
        else:
            print("[FAST-WHISPER] Nothing to save.", flush=True)
        raise

    _write_txt_output(dst, translated, para_indices)
    duration = time.time() - start_timer
    hours, rem = divmod(duration, 3600)
    minutes, seconds = divmod(rem, 60)
    print(f"\n\n[FAST-WHISPER] Translation finished in: {int(hours)}:{int(minutes):02}:{seconds:06.3f}", flush=True)
    return True


def process_txt_batch(job_file, ctx=3, target="de"):
    start_timer = time.time()
    jobs = []
    with open(job_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = [p.strip().strip('"') for p in line.split("|")]
            if len(parts) >= 2 and parts[0] and parts[1]:
                jobs.append((parts[0], parts[1]))
    total = len(jobs)
    if total == 0:
        print("[FAST-WHISPER] No TXT translation jobs.", flush=True)
        return
    for idx, (src, dst) in enumerate(jobs, 1):
        prefix = "" if idx == 1 else "\n"
        print(f"{prefix}[FAST-WHISPER] Translating ({idx} of {total}): {os.path.basename(src)}", flush=True)
        print(flush=True)
        if not os.path.exists(src):
            print(f"[FAST-WHISPER] Source file missing: {os.path.basename(src)}", flush=True)
            continue
        if os.path.exists(dst):
            print(f"[FAST-WHISPER] Target already exists - skipped: {os.path.basename(dst)}", flush=True)
            continue
        try:
            created = process_txt_file(src, dst, ctx, target)
        except TranslationError as e:
            print(f"[FAST-WHISPER] Translation aborted: {e}", flush=True)
            print(f"[FAST-WHISPER] Batch aborted - {idx - 1} of {total} file(s) completed.", flush=True)
            raise
        if created:
            print(f"\n[FAST-WHISPER] Translation created: {os.path.basename(dst)}", flush=True)
        else:
            print(f"\n[FAST-WHISPER] Nothing written: {os.path.basename(dst)}", flush=True)
    duration = time.time() - start_timer
    hours, rem = divmod(duration, 3600)
    minutes, seconds = divmod(rem, 60)
    print(f"\n[FAST-WHISPER] All TXT translations finished in: {int(hours)}:{int(minutes):02}:{seconds:06.3f}\n", flush=True)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--sentence-srt":
        audio = sys.argv[4] if len(sys.argv) >= 5 else None
        split_srt_from_words(sys.argv[2], sys.argv[3], audio, max_chars=240, max_line=1000)
        sys.exit(0)
    if len(sys.argv) >= 4 and sys.argv[1] == "--words-srt":
        audio = sys.argv[4] if len(sys.argv) >= 5 else None
        split_srt_from_words(sys.argv[2], sys.argv[3], audio)
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--split-srt":
        split_srt_file(sys.argv[2])
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--split-srt-display":
        split_srt_file(sys.argv[2], 80)
        sys.exit(0)
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--txt":
        src = sys.argv[2]
        dst = sys.argv[3] if len(sys.argv) >= 4 else src
        ctx = int(sys.argv[4]) if len(sys.argv) >= 5 else 3
        target = sys.argv[5].lower() if len(sys.argv) >= 6 else "de"
        if len(sys.argv) >= 7:
            MODEL = sys.argv[6]
        if target not in ("de", "en"):
            print(f"[FAST-WHISPER] Unknown target '{target}'. Use 'de' or 'en'.", flush=True)
            sys.exit(1)
        if not os.path.exists(src):
            print(f"[FAST-WHISPER] Source file not found: {src}", flush=True)
            sys.exit(1)
        setup_ollama()
        try:
            process_txt_file(src, dst, ctx, target)
        except TranslationError as e:
            print("[FAST-WHISPER] Translation aborted - see error above.", flush=True)
            stop_ollama()
            sys.exit(1)
        stop_ollama()
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--txt-batch":
        job = sys.argv[2]
        ctx = int(sys.argv[3]) if len(sys.argv) >= 4 else 3
        target = sys.argv[4].lower() if len(sys.argv) >= 5 else "de"
        if len(sys.argv) >= 6:
            MODEL = sys.argv[5]
        if target not in ("de", "en"):
            print(f"[FAST-WHISPER] Unknown target '{target}'. Use 'de' or 'en'.", flush=True)
            sys.exit(1)
        if not os.path.exists(job):
            print(f"[FAST-WHISPER] Job file not found: {job}", flush=True)
            sys.exit(1)
        setup_ollama()
        try:
            process_txt_batch(job, ctx, target)
        except TranslationError as e:
            print("[FAST-WHISPER] Translation aborted - see error above.", flush=True)
            stop_ollama()
            sys.exit(1)
        stop_ollama()
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--batch":
        ctx = int(sys.argv[3]) if len(sys.argv) >= 4 else 3
        if len(sys.argv) >= 5:
            MODEL = sys.argv[4]
        setup_ollama()
        try:
            process_batch(sys.argv[2], ctx)
        except TranslationError as e:
            print("[FAST-WHISPER] Translation aborted - see error above.", flush=True)
            stop_ollama()
            sys.exit(1)
        stop_ollama()
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--clause-batch":
        ctx = int(sys.argv[3]) if len(sys.argv) >= 4 else 3
        if len(sys.argv) >= 5:
            MODEL = sys.argv[4]
        setup_ollama()
        try:
            process_batch(sys.argv[2], ctx, clause_mode=True)
        except TranslationError as e:
            print("[FAST-WHISPER] Translation aborted - see error above.", flush=True)
            stop_ollama()
            sys.exit(1)
        stop_ollama()
        sys.exit(0)

    if len(sys.argv) >= 3:
        ctx = int(sys.argv[3]) if len(sys.argv) >= 4 else 3
        if len(sys.argv) >= 5:
            MODEL = sys.argv[4]
        if len(sys.argv) >= 6:
            TARGET_LANG = sys.argv[5].lower()
        setup_ollama()
        try:
            process_srt(sys.argv[1], sys.argv[2], ctx)
        except TranslationError as e:
            print("[FAST-WHISPER] Translation aborted - see error above.", flush=True)
            stop_ollama()
            sys.exit(1)
        stop_ollama()
