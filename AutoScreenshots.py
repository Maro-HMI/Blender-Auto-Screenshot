bl_info = {
    "name": "Auto Screenshots (Timelapse + Fast OpenGL)",
    "author": "Maro and ChatGPT",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "3D View > Sidebar (N) > Auto Screenshots",
    "description": "Fast, non-blocking timelapse screenshots via OpenGL, with skip-unchanged and MP4 assembly.",
    "category": "3D View",
}

import bpy
import os
import shutil
import hashlib
import subprocess
import platform
from datetime import datetime
from bpy.props import (
    StringProperty,
    IntProperty,
    EnumProperty,
    BoolProperty,
)
from bpy.types import Operator, Panel, PropertyGroup


_RUNNING = False
_TIMER = None
_LAST_FP = None


# ---------------------------------------------------------
# Utility
# ---------------------------------------------------------

def _timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def _resolve_dir(user_dir: str):
    d = user_dir.strip()
    if not d:
        d = "//timelapse"
    return bpy.path.abspath(d)

def _ensure_exists_after_success(path):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def _open_folder(path):
    """Cross-platform folder opener."""
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

    system = platform.system()
    if system == "Windows":
        os.startfile(path)
    elif system == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _find_viewport_region():
    wm = bpy.context.window_manager
    for win in wm.windows:
        scr = win.screen
        if not scr:
            continue
        for area in scr.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "WINDOW":
                        return win, area, region
    return None, None, None

def _dims(key):
    return (1920, 1080) if key == "1080p" else (1280, 720)


# ---------------------------------------------------------
# Fast OpenGL JPEG Capture
# ---------------------------------------------------------

def _capture_jpeg(path, width, height, quality):
    win, area, region = _find_viewport_region()
    if not win:
        return False

    ctx = bpy.context
    scene = ctx.scene
    r = scene.render
    imgset = r.image_settings

    old = {
        "filepath": r.filepath,
        "file_format": imgset.file_format,
        "color_mode": imgset.color_mode,
        "quality": imgset.quality,
        "rx": r.resolution_x,
        "ry": r.resolution_y,
        "rp": r.resolution_percentage,
        "ufe": r.use_file_extension,
    }

    r.filepath = path
    r.use_file_extension = True
    imgset.file_format = 'JPEG'
    imgset.color_mode = 'RGB'
    imgset.quality = quality
    r.resolution_x = width
    r.resolution_y = height
    r.resolution_percentage = 100

    ok = True
    try:
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.render.opengl(write_still=True, view_context=True)
    except Exception:
        ok = False

    # Restore
    r.filepath = old["filepath"]
    imgset.file_format = old["file_format"]
    imgset.color_mode = old["color_mode"]
    imgset.quality = old["quality"]
    r.resolution_x = old["rx"]
    r.resolution_y = old["ry"]
    r.resolution_percentage = old["rp"]
    r.use_file_extension = old["ufe"]

    return ok and os.path.exists(path)


# ---------------------------------------------------------
# High Sensitivity Fingerprint (128×72)
# ---------------------------------------------------------

def _quick_viewport_fingerprint():
    win, area, region = _find_viewport_region()
    if not win:
        return None

    ctx = bpy.context
    scene = ctx.scene
    r = scene.render
    imgset = r.image_settings

    temp_path = os.path.join(bpy.app.tempdir, "tl_fingerprint.jpg")

    old = {
        "filepath": r.filepath,
        "file_format": imgset.file_format,
        "color_mode": imgset.color_mode,
        "quality": imgset.quality,
        "rx": r.resolution_x,
        "ry": r.resolution_y,
        "rp": r.resolution_percentage,
        "ufe": r.use_file_extension,
    }

    r.filepath = temp_path
    r.use_file_extension = True
    imgset.file_format = 'JPEG'
    imgset.color_mode = 'RGB'
    imgset.quality = 70
    r.resolution_x = 128
    r.resolution_y = 72
    r.resolution_percentage = 100

    fp = None

    try:
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.render.opengl(write_still=True, view_context=True)
    except Exception:
        fp = None
    else:
        if os.path.exists(temp_path):
            try:
                with open(temp_path, "rb") as f:
                    fp = hashlib.md5(f.read()).hexdigest()
            except:
                fp = None
            try:
                os.remove(temp_path)
            except:
                pass

    r.filepath = old["filepath"]
    imgset.file_format = old["file_format"]
    imgset.color_mode = old["color_mode"]
    imgset.quality = old["quality"]
    r.resolution_x = old["rx"]
    r.resolution_y = old["ry"]
    r.resolution_percentage = old["rp"]
    r.use_file_extension = old["ufe"]

    return fp


# ---------------------------------------------------------
# Scene Properties
# ---------------------------------------------------------

class TL_Props(PropertyGroup):
    output_dir: StringProperty(
        name="Folder",
        description="Folder where screenshots are saved (empty = //timelapse next to .blend)",
        subtype="DIR_PATH",
        default=""
    )
    prefix: StringProperty(
        name="Prefix",
        description="Filename prefix for each screenshot",
        default="snap"
    )
    interval: IntProperty(
        name="Interval (seconds)",
        description="Time between screenshots",
        min=1,
        default=10
    )
    jpeg_quality: IntProperty(
        name="JPEG Quality",
        description="Output image quality",
        min=1, max=100,
        default=70
    )
    resolution: EnumProperty(
        name="Resolution",
        description="Final screenshot resolution",
        items=[('1080p', "1920×1080", ""), ('720p', "1280×720", "")],
        default='1080p'
    )
    mp4_fps: IntProperty(
        name="MP4 FPS",
        description="Frames per second for MP4",
        min=1, max=120,
        default=24
    )
    mp4_quality: EnumProperty(
        name="MP4 Quality",
        description="H.264 quality",
        items=[('LOW', "Low", ""), ('MEDIUM', "Medium", ""), ('HIGH', "High", "")],
        default='MEDIUM'
    )
    skip_unchanged: BoolProperty(
        name="Skip Unchanged Frames",
        description="Avoid saving images when nothing visually changes",
        default=True
    )


def _props():
    return bpy.context.scene.timelapse_props


# ---------------------------------------------------------
# Pre-start test
# ---------------------------------------------------------

def _test_capture():
    p = _props()
    width, height = _dims(p.resolution)
    temp_path = os.path.join(bpy.app.tempdir, "timelapse_test.jpg")
    ok = _capture_jpeg(temp_path, width, height, p.jpeg_quality)
    exists = os.path.exists(temp_path)
    if exists:
        os.remove(temp_path)
    return ok and exists


# ---------------------------------------------------------
# Start / Stop operators
# ---------------------------------------------------------

class VIEW3D_OT_timelapse_start(Operator):
    bl_idname = "view3d.timelapse_start"
    bl_label = "Start Screenshots"
    bl_description = "Begin automatic screenshot timelapse"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        global _RUNNING, _TIMER, _LAST_FP

        if not bpy.data.filepath:
            self.report({'ERROR'}, "Save your .blend file first.")
            return {'CANCELLED'}

        if _RUNNING:
            return {'CANCELLED'}

        if not _test_capture():
            self.report({'ERROR'},
                        "Screenshot failed. On macOS, ensure Blender has Full Disk Access.")
            return {'CANCELLED'}

        _LAST_FP = None
        p = _props()

        _RUNNING = True
        _TIMER = context.window_manager.event_timer_add(
            time_step=float(p.interval),
            window=context.window
        )
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        global _RUNNING, _LAST_FP
        if not _RUNNING:
            return {'CANCELLED'}

        if event.type == "TIMER":
            p = _props()

            # Skip unchanged?
            if p.skip_unchanged:
                fp = _quick_viewport_fingerprint()
                if fp is not None and fp == _LAST_FP:
                    return {'PASS_THROUGH'}
                _LAST_FP = fp

            width, height = _dims(p.resolution)
            temp_path = os.path.join(
                bpy.app.tempdir,
                f"{p.prefix}_{_timestamp()}.jpg"
            )

            ok = _capture_jpeg(temp_path, width, height, p.jpeg_quality)

            if ok and os.path.exists(temp_path):
                out_dir = _resolve_dir(p.output_dir)
                _ensure_exists_after_success(out_dir)
                final_path = os.path.join(out_dir, os.path.basename(temp_path))
                try:
                    os.replace(temp_path, final_path)
                except:
                    shutil.copy2(temp_path, final_path)
                    os.remove(temp_path)

        return {'PASS_THROUGH'}

    def cancel(self, context):
        global _RUNNING, _TIMER
        if _TIMER:
            context.window_manager.event_timer_remove(_TIMER)
        _RUNNING = False



class VIEW3D_OT_timelapse_stop(Operator):
    bl_idname = "view3d.timelapse_stop"
    bl_label = "Stop Screenshots"
    bl_description = "Stop automatic screenshot recording"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        global _RUNNING, _TIMER
        if _TIMER:
            context.window_manager.event_timer_remove(_TIMER)
        _RUNNING = False
        return {'FINISHED'}


# ---------------------------------------------------------
# MP4 Tools
# ---------------------------------------------------------

def _gather(directory, prefix):
    directory = _resolve_dir(directory)
    if not os.path.exists(directory):
        return directory, []
    files = sorted(
        f for f in os.listdir(directory)
        if f.startswith(prefix) and f.lower().endswith(".jpg")
    )
    return directory, files


def _make_mp4(directory, prefix, width, height, fps, crf, report):
    directory, files = _gather(directory, prefix)
    if not files:
        report({'ERROR'}, "No JPG files found.")
        return False

    scene_name = "TL_MP4_SCENE"
    scene = bpy.data.scenes.get(scene_name) or bpy.data.scenes.new(scene_name)

    r = scene.render
    r.resolution_x = width
    r.resolution_y = height
    r.fps = fps
    r.use_file_extension = True
    r.image_settings.file_format = 'FFMPEG'

    ff = r.ffmpeg
    ff.format = 'MPEG4'
    ff.codec = 'H264'
    ff.constant_rate_factor = crf
    ff.audio_codec = 'NONE'

    se = scene.sequence_editor or scene.sequence_editor_create()
    for s in list(se.sequences_all):
        se.sequences.remove(s)

    first = os.path.join(directory, files[0])
    strip = se.sequences.new_image("TL", filepath=first, channel=1, frame_start=1)

    for fname in files[1:]:
        strip.elements.append(filename=fname)

    strip.directory = directory + os.sep

    scene.frame_start = 1
    scene.frame_end = len(strip.elements)

    out = os.path.join(directory, f"{prefix}_{_timestamp()}")
    r.filepath = out

    bpy.ops.render.render(animation=True, scene=scene_name)
    report({'INFO'}, f"Saved: {out}.mp4")
    return True


class VIEW3D_OT_timelapse_make_mp4(Operator):
    bl_idname = "view3d.timelapse_make_mp4"
    bl_label = "Make MP4"
    bl_description = "Assemble all screenshots into an MP4 timelapse"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        p = _props()
        width, height = _dims(p.resolution)
        ok = _make_mp4(
            p.output_dir, p.prefix, width, height, p.mp4_fps, p.mp4_quality,
            self.report
        )
        return {'FINISHED'} if ok else {'CANCELLED'}



# ---------------------------------------------------------
# NEW: Open Screenshot Folder
# ---------------------------------------------------------

class VIEW3D_OT_timelapse_open_folder(Operator):
    bl_idname = "view3d.timelapse_open_folder"
    bl_label = "Open Folder"
    bl_description = "Open the screenshot output folder"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        p = _props()
        folder = _resolve_dir(p.output_dir)
        _open_folder(folder)
        return {'FINISHED'}


# ---------------------------------------------------------
# UI PANELS
# ---------------------------------------------------------

class VIEW3D_PT_timelapse(Panel):
    bl_label = "Auto Screenshots"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Auto Screenshots"

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        row.scale_y = 1.5

        if _RUNNING:
            row.alert = True
            row.operator(
                "view3d.timelapse_stop",
                text="Stop Screenshots",
                icon="PAUSE"
            )
        else:
            row.operator(
                "view3d.timelapse_start",
                text="Start Screenshots",
                icon="REC"
            )


class VIEW3D_PT_timelapse_options(Panel):
    bl_label = "Options"
    bl_parent_id = "VIEW3D_PT_timelapse"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Auto Screenshots"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        p = _props()
        layout = self.layout

        col = layout.column(align=True)
        col.prop(p, "output_dir")
        col.prop(p, "prefix")
        col.prop(p, "interval")
        col.prop(p, "jpeg_quality")
        col.prop(p, "resolution")
        col.prop(p, "skip_unchanged")

        layout.separator()
        col = layout.column(align=True)
        col.prop(p, "mp4_fps")
        col.prop(p, "mp4_quality")
        col.operator(
            "view3d.timelapse_make_mp4",
            text="Make MP4",
            icon="RENDER_ANIMATION"
        )

        layout.separator()

        # NEW: Open Folder Button
        layout.operator(
            "view3d.timelapse_open_folder",
            text="Open Screenshot Folder",
            icon="FILE_FOLDER"
        )


# ---------------------------------------------------------
# Header Badge
# ---------------------------------------------------------

def _header_badge(self, context):
    if not _RUNNING:
        row = self.layout.row()
        row.alert = True
        row.label(text="NOT RECORDING")


# ---------------------------------------------------------
# Registration
# ---------------------------------------------------------

classes = (
    TL_Props,
    VIEW3D_OT_timelapse_start,
    VIEW3D_OT_timelapse_stop,
    VIEW3D_OT_timelapse_make_mp4,
    VIEW3D_OT_timelapse_open_folder,
    VIEW3D_PT_timelapse,
    VIEW3D_PT_timelapse_options,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.timelapse_props = bpy.props.PointerProperty(type=TL_Props)
    bpy.types.VIEW3D_HT_header.prepend(_header_badge)

def unregister():
    bpy.types.VIEW3D_HT_header.remove(_header_badge)
    del bpy.types.Scene.timelapse_props
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()
