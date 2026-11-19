"""Whisper.cpp helper: colored ASCII banner and whisper-cli progress runner."""
import os
import re
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))

WORD = "WHISPER.CPP"
GLYPHS = {
    "W": ("#   #", "#   #", "#   #", "# # #", " # # "),
    "H": ("#   #", "#   #", "#####", "#   #", "#   #"),
    "I": ("#####", "  #  ", "  #  ", "  #  ", "#####"),
    "S": (" ####", "#    ", " ### ", "    #", "#### "),
    "P": ("#### ", "#   #", "#### ", "#    ", "#    "),
    "E": ("#####", "#    ", "#### ", "#    ", "#####"),
    "R": ("#### ", "#   #", "#### ", "#  # ", "#   #"),
    ".": ("     ", "     ", "     ", "     ", "  #  "),
    "C": (" ####", "#    ", "#    ", "#    ", " ####"),
}
COLORS = (4, 14, 2, 11, 1, 13, 15, 8, 11, 13, 14)  # Windows console attributes


def _console_attr(handle, attr):
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTextAttribute(handle, attr)
        return True
    except Exception:
        return False


def print_banner():
    rows = []
    for row in range(5):
        rows.append([(GLYPHS[ch][row], ch, idx) for idx, ch in enumerate(WORD)])

    use_color = sys.stdout.isatty() and os.name == "nt"
    if not use_color:
        for row in rows:
            print("  " + " ".join(g for g, _, _ in row))
        print()
        return

    import ctypes
    handle = ctypes.windll.kernel32.GetStdHandle(-11)
    for row in rows:
        _console_attr(handle, 7)
        sys.stdout.write("  ")
        for glyph, ch, idx in row:
            _console_attr(handle, COLORS[idx])
            sys.stdout.write(glyph + " ")
            sys.stdout.flush()
        _console_attr(handle, 7)
        sys.stdout.write("\n")
        sys.stdout.flush()
    sys.stdout.flush()
    print()


def run_whisper(args_file, stdout_log):
    exe = os.path.join(BASE, "data", "whisper-cli.exe")
    if not os.path.exists(exe):
        print(f"whisper-cli not found: {exe}", file=sys.stderr)
        return 1
    if not os.path.exists(args_file):
        print(f"args file not found: {args_file}", file=sys.stderr)
        return 1

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    ticker_stop = threading.Event()
    progress_seen = threading.Event()
    ticker_state = {"used": False}
    out_lock = threading.Lock()
    started = time.monotonic()

    def ticker():
        while not ticker_stop.wait(0.25):
            if progress_seen.is_set():
                continue
            with out_lock:
                try:
                    sys.stdout.write(f"\r  Whisper: {time.monotonic() - started:4.1f}s ...")
                    sys.stdout.flush()
                    ticker_state["used"] = True
                except Exception:
                    return

    ticker_thread = threading.Thread(target=ticker, daemon=True)
    ticker_thread.start()
    other = []

    with open(stdout_log, "wb") as out:
        proc = subprocess.Popen(
            [exe, "@" + args_file],
            cwd=BASE,
            stdout=out,
            stderr=subprocess.PIPE,
            creationflags=flags,
        )
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            m = re.search(r"progress\s*=\s*(\d+)%", line)
            if m:
                pct = int(m.group(1))
                progress_seen.set()
                with out_lock:
                    if ticker_state["used"]:
                        sys.stdout.write("\r" + " " * 60 + "\r")
                        sys.stdout.flush()
                        ticker_state["used"] = False
                    print(f"  Whisper progress: {pct:3d}%", flush=True)
            elif line.strip():
                other.append(line)
        proc.wait()
    ticker_stop.set()
    ticker_thread.join(timeout=1.0)
    with out_lock:
        if ticker_state["used"]:
            sys.stdout.write("\r" + " " * 60 + "\r\n")
            sys.stdout.flush()

    if proc.returncode != 0:
        for line in other:
            print(line)
    return proc.returncode


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "banner":
        print_banner()
        return 0
    if len(sys.argv) >= 4 and sys.argv[1] == "run":
        return run_whisper(sys.argv[2], sys.argv[3])
    print("usage: helper.py banner | helper.py run <argsfile> <stdoutlog>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
