"""Agent-side slicing for PrintWatch (v0.5.0).

Model file(s) in -> PrusaSlicer headless (bundled fleet profile) ->
firmware-safe Anycubic .gcode.3mf package out, with slice stats.

Design contract:
- SLICING IS SAFE: it only computes files on the agent host. No tool that
  calls into this module may move hardware; upload+print remains behind the
  pre_tool_call approval gate (printer_upload).
- The bundled profile is the ONLY profile this module uses. Never invent
  machine/filament values here; profile provenance lives in the repo
  profiles/ directory and the 3d-printing skill.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
PROFILE_INI = PLUGIN_DIR / "profiles" / "kobra-v2-prusa.ini"
SUPPORTED_EXT = {".stl", ".3mf", ".obj"}

_FILAMENT_RE = re.compile(r"; filament used \[cm3\]\s*=\s*([0-9.]+)")
_TIME_RE = re.compile(r"; estimated printing time.*?=\s*(.+)")


def _check_slicer() -> str:
    exe = shutil.which("prusa-slicer")
    if not exe:
        raise RuntimeError(
            "prusa-slicer binary not found on PATH — install it with "
            "'sudo apt install prusa-slicer' on the agent host (v0.5.0 "
            "slicing is a host-side capability)."
        )
    return exe


def _validate_model_paths(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        f = Path(str(p)).expanduser()
        if not f.is_file():
            raise FileNotFoundError(f"model file not found: {f}")
        if f.suffix.lower() not in SUPPORTED_EXT:
            raise ValueError(
                f"unsupported model type {f.suffix!r} for {f.name} "
                f"(supported: {', '.join(sorted(SUPPORTED_EXT))})"
            )
        out.append(f)
    if not out:
        raise ValueError("no model files given")
    return out


def _parse_stats(gcode_path: Path) -> dict:
    layers = 0
    fil_cm3 = 0.0
    est = ""
    with gcode_path.open(errors="replace") as fh:
        for line in fh:
            if line.startswith("G1") and " Z" in line:
                layers += 1
            elif "; filament used [cm3]" in line:
                m = _FILAMENT_RE.search(line)
                if m:
                    fil_cm3 = max(fil_cm3, float(m.group(1)))
            elif "; estimated printing time" in line:
                m = _TIME_RE.search(line)
                if m:
                    est = m.group(1).strip()
    return {"layers": layers, "filament_cm3": fil_cm3, "est_time": est}


def _fmt_time(hms: str) -> str:
    """Prusa emits '1h 2m 3s' style suffixes after the numeric field; keep as-is."""
    return hms if hms else "unknown"


def slice_models(models: list[str], out_name: str | None = None,
                 center: str = "127.5,127.5", workdir: str | None = None) -> dict:
    """Slice one or more model files into a fleet-ready Anycubic package.

    Multiple model files slice as ONE combined plate (verified PrusaSlicer
    CLI behavior). `workdir` overrides the output directory (callers inside
    Hermes inject plugin_data_dir; standalone defaults to ./_slice_jobs).
    Returns a dict with paths + stats; raises on failure.
    """
    try:
        from kobra_dialect import build_package
    except ImportError:
        from .kobra_dialect import build_package  # installed-as-package import

    exe = _check_slicer()
    model_paths = _validate_model_paths(models)

    if not PROFILE_INI.is_file():
        raise RuntimeError(
            f"bundled profile missing: {PROFILE_INI} (plugin install is "
            "incomplete — reinstall the plugin)"
        )

    out_dir = Path(workdir) if workdir else (PLUGIN_DIR / "_slice_jobs")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = out_name or f"slice-{stamp}"
    # keep the name filesystem-safe
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:80]
    raw_gcode = out_dir / f"{base}.gcode"

    cmd = [exe, "--load", str(PROFILE_INI), "--center", center,
           "--export-gcode", "-o", str(raw_gcode)] + [str(p) for p in model_paths]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    if proc.returncode != 0 or not raw_gcode.is_file():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            "prusa-slicer failed (rc=" + str(proc.returncode) + "): "
            + (" | ".join(tail[-5:]) if tail else "no output")
        )

    stats = _parse_stats(raw_gcode)

    # NOTE: build_package strips any `; filament_colour_type` lines itself
    package = out_dir / f"{base}.gcode.3mf"
    build_package(str(raw_gcode), str(PLUGIN_DIR / "profiles"), str(package),
                  model_name=base)

    out_names = ", ".join(p.name for p in model_paths)
    return {
        "package": str(package),
        "raw_gcode": str(raw_gcode),
        "sources": out_names,
        "layers": stats["layers"],
        "filament_cm3": stats["filament_cm3"],
        "est_time": _fmt_time(stats["est_time"]),
        "profile": "Kobra 3 V2 (bundled)",
    }
