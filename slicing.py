"""PrintWatch headless slicing — STL -> project-3mf -> OrcaSlicer CLI -> gcode.

Recipe adapted from 3DCreationsByChad/orca-auto (MIT) headless-slicing findings:
standalone profile files fail the vendor-bundle compatibility check, so we build
a project .3mf with an EMBEDDED resolved config and slice that (bypasses the
check). Profiles: official Anycubic Kobra 3 V2 set merged from OrcaSlicer main
(PR #15229, merged 2026-09-22).

Fleet gotchas handled here:
- use_relative_e_distances=1 -> before_layer_change_gcode gains "G92 E0"
- retraction_distances_when_cut pinned into [10,18] (CLI validates strictly)
- "; filament_colour_type" line stripped post-slice (crashes Kobra Go firmware)
"""
from __future__ import annotations

import json
import shutil
import subprocess as sp
import tempfile
import time
import zipfile
from pathlib import Path

# The one Xvfb display we own while slicing
_DISPLAY = ":99"


def load_flat_profile(profile_dir: str | Path) -> dict:
    """Merge the three flattened Kobra 3 V2 presets into one project config."""
    pdir = Path(profile_dir)
    cfg: dict = {}
    for name in (
        "Anycubic Kobra 3 V2 0.4 nozzle.json",                       # machine
        "Anycubic PLA @Anycubic Kobra 3 V2 0.4 nozzle.json",         # filament
        "0.20mm Standard @Anycubic Kobra 3 V2 0.4 nozzle.json",      # process (wins)
    ):
        cfg.update(json.loads((pdir / name).read_text()))
    # CLI hard-validates these ranges even when the feature is disabled
    if cfg.get("enable_long_retraction_when_cut") in ("0", 0, None):
        cfg["long_retractions_when_cut"] = ["12"]
        cfg["retraction_distances_when_cut"] = ["12"]
    # relative-E requires an extruder reset at every layer change
    if str(cfg.get("use_relative_e_distances", "0")) == "1":
        blc = cfg.get("before_layer_change_gcode") or ""
        if "G92 E0" not in blc:
            cfg["before_layer_change_gcode"] = (blc + "\nG92 E0").strip()
    return cfg


# ---------------------------------------------------------------- 3MF packaging
# (zip layout per orca-auto threemf_builder, MIT (c) 3DCreationsByChad)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
)
_MODEL_NS = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
)


def _mesh_model_xml(stl_path: Path) -> str:
    import trimesh

    mesh = trimesh.load(str(stl_path), force="mesh")
    verts = "".join(
        f'<vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>' for x, y, z in mesh.vertices
    )
    tris = "".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" {_MODEL_NS} requiredextensions="p">'
        f"<resources><object id=\"1\" type=\"model\"><mesh>"
        f"<vertices>{verts}</vertices><triangles>{tris}</triangles>"
        "</mesh></object></resources><build/></model>"
    )


def _model3d_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" {_MODEL_NS} requiredextensions="p">'
        '<metadata name="Application">BambuStudio-2.3.1</metadata>'
        '<metadata name="BambuStudio:3mfVersion">1</metadata>'
        '<resources><object id="2" type="model"><components>'
        '<component p:path="/3D/Objects/obj_1.model" objectid="1" '
        'transform="1 0 0 0 1 0 0 0 1 40 40 0"/></components></object></resources>'
        '<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 40 40 0" '
        'printable="1"/></build></model>'
    )


def _model3d_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/Objects/obj_1.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    )


def _model_settings_xml(stl_path: Path) -> str:
    from xml.sax.saxutils import escape

    name = escape(Path(stl_path).stem)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>'
        f'<object id="2"><metadata key="name" value="{name}"/>'
        '<metadata key="extruder" value="1"/>'
        f'<part id="1" subtype="normal_part"><metadata key="name" value="{name}"/>'
        '<mesh_stat edges_fixed="0" degenerate_facets="0" facets_removed="0" '
        'facets_reversed="0" backwards_edges="0"/></part></object>'
        '<plate><metadata key="plater_id" value="1"/>'
        '<metadata key="filament_map_mode" value="Auto For Flush"/>'
        '<metadata key="filament_maps" value="1"/>'
        '<model_instance><metadata key="object_id" value="2"/>'
        '<metadata key="instance_id" value="0"/></model_instance></plate></config>'
    )


def build_project_3mf(stl_path: str | Path, out_path: str | Path, config: dict) -> Path:
    stl = Path(stl_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("3D/3dmodel.model", _model3d_xml())
        z.writestr("3D/_rels/3dmodel.model.rels", _model3d_rels())
        z.writestr("3D/Objects/obj_1.model", _mesh_model_xml(stl))
        z.writestr("Metadata/model_settings.config", _model_settings_xml(stl))
        z.writestr("Metadata/project_settings.config", json.dumps(config))
    return out


# ---------------------------------------------------------------- slicing


def _xvfb(display: str):
    """Start Xvfb; returns the popen handle (caller terminates)."""
    proc = sp.Popen(
        ["Xvfb", display, "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
        stdout=sp.DEVNULL, stderr=sp.DEVNULL,
    )
    time.sleep(1.0)
    return proc


def slice_project(
    in_3mf: str | Path,
    out_3mf: str | Path,
    orca_bin: str | Path,
    datadir: str | Path | None = None,
    timeout: float = 300.0,
) -> Path:
    """Slice a project 3mf headlessly (spawns its own Xvfb). Returns out 3mf."""
    in_p, out_p = Path(in_3mf), Path(out_3mf)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    tmp_datadir = None
    xv = _xvfb(_DISPLAY)
    try:
        if datadir is None:
            tmp_datadir = tempfile.mkdtemp(prefix="orca-data-")
            datadir = tmp_datadir
        cmd = [str(orca_bin), "--datadir", str(datadir),
               "--arrange", "1", "--slice", "0",
               "--export-3mf", str(out_p), str(in_p)]
        import os
        env = dict(os.environ)
        env["DISPLAY"] = _DISPLAY
        r = sp.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        result_file = out_p.parent / "result.json"
        ok = r.returncode == 0 and out_p.exists() and out_p.stat().st_size > 10_000
        if not ok:
            detail = ""
            if result_file.exists():
                try:
                    detail = json.loads(result_file.read_text()).get("error_string", "")
                except Exception:
                    pass
            raise RuntimeError(
                f"orca-slicer failed (rc={r.returncode}) {detail} "
                f"| stderr tail: {(r.stderr or '')[-300:]}"
            )
        return out_p
    finally:
        xv.terminate()
        if tmp_datadir:
            shutil.rmtree(tmp_datadir, ignore_errors=True)


def extract_gcode(sliced_3mf: str | Path, out_gcode: str | Path) -> Path:
    """Pull the first embedded .gcode out of a sliced project 3mf, post-processed."""
    out = Path(out_gcode)
    with zipfile.ZipFile(sliced_3mf) as z:
        names = [n for n in z.namelist() if n.endswith(".gcode")]
        if not names:
            raise RuntimeError(f"no .gcode inside {sliced_3mf}")
        data = z.read(names[0]).decode(errors="replace")
    # Kobra firmware crasher: drop the filament_colour_type comment line entirely
    lines = [ln for ln in data.splitlines()
             if not ln.strip().startswith(";") or "filament_colour_type" not in ln]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    return out


def slice_stl_prusa(stl_path: str, workdir: str, out_name: str = "print",
                    ini_path: str = "/home/krilla/printwatch-staging/kobra-v2.ini",
                    prusa_bin: str = "prusa-slicer") -> dict:
    """PRIMARY slice path: PrusaSlicer headless (verified 2026-11-06).

    kobra-v2.ini = converted from Anycubic's own Slicer-Next export
    (tahkingle/AnycubicKobra3v2PrusaProfile) + official Orca process/filament
    values. PrusaSlicer CLI has no vendor-bundle wall — plain ini works.
    """
    import os
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    out_gcode = wd / f"{out_name}.gcode"
    cmd = [prusa_bin, "--load", str(ini_path),
           "--center", "127.5,127.5",
           "--export-gcode", "-o", str(out_gcode), str(stl_path)]
    r = sp.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not out_gcode.exists():
        raise RuntimeError(f"prusa-slicer failed (rc={r.returncode}): {(r.stderr or r.stdout or '')[-300:]}")
    # post-process: strip the Kobra firmware crasher line if present
    data = out_gcode.read_text(errors="replace")
    cleaned = "\n".join(
        ln for ln in data.splitlines()
        if not (ln.strip().startswith(";") and "filament_colour_type" in ln)
    )
    out_gcode.write_text(cleaned)
    return {"gcode": str(out_gcode), "bytes": out_gcode.stat().st_size,
            "slicer": "prusa-slicer 2.9.4", "profile": ini_path}


def slice_stl(stl_path: str, workdir: str, orca_bin: str, profile_dir: str,
              name: str = "print") -> dict:
    """Full pipeline: STL -> sliced gcode (+ project 3mf kept for upload)."""
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    cfg = load_flat_profile(profile_dir)
    proj = build_project_3mf(stl_path, wd / f"{name}.project.3mf", cfg)
    sliced = slice_project(proj, wd / f"{name}.sliced.3mf", orca_bin)
    gcode = extract_gcode(sliced, wd / f"{name}.gcode")
    return {"project_3mf": str(proj), "sliced_3mf": str(sliced), "gcode": str(gcode)}
