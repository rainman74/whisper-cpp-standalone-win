"""Whisper.cpp helper: colored ASCII banner and whisper-cli progress runner."""
import os
import re
import subprocess
import sys
import json

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

    out_base = None
    try:
        with open(args_file, "r", encoding="utf-8") as af:
            argv = [ln.rstrip("\r\n") for ln in af]
        if "-of" in argv and argv.index("-of") + 1 < len(argv):
            out_base = argv[argv.index("-of") + 1]
    except OSError:
        out_base = None
    vad_map_path = (out_base + ".json.vadmap") if out_base else None
    if vad_map_path and os.path.exists(vad_map_path):
        try:
            os.remove(vad_map_path)
        except OSError:
            pass
    vad_segments = []

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    other = []
    first = True

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
            if "vad_segment_info:" in line:
                vm = re.search(r"orig_start:\s*([0-9.]+),\s*orig_end:\s*([0-9.]+),\s*vad_start:\s*([0-9.]+),\s*vad_end:\s*([0-9.]+)", line)
                if vm:
                    vad_segments.append([float(vm.group(1)), float(vm.group(2)), float(vm.group(3)), float(vm.group(4))])
            m = re.search(r"progress\s*=\s*(\d+)%", line)
            if m:
                pct = int(m.group(1))
                if first:
                    print(f"  Whisper progress: {pct:3d}%", end="", flush=True)
                    first = False
                else:
                    print(f"\r  Whisper progress: {pct:3d}%", end="", flush=True)
            elif line.strip() and any(k in line.lower() for k in ("error", "failed", "warning")):
                other.append(line)
        proc.wait()
    if vad_map_path:
        try:
            if vad_segments:
                with open(vad_map_path, "w", encoding="ascii") as vf:
                    json.dump(vad_segments, vf)
            elif os.path.exists(vad_map_path):
                os.remove(vad_map_path)
        except OSError:
            pass
    if not first:
        print(flush=True)

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
