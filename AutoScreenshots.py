bl_info = {
    "name": "Auto Screenshots (Timelapse + Fast OpenGL)",
    "author": "Maro, Matt and ChatGPT",
    "version": (1, 1, 3),
    "blender": (4, 5, 0),
    "location": "3D View > Sidebar (N) > Auto Screenshots",
    "description": "Fast, non-blocking OpenGL timelapse screenshots with smart idle detection and MP4 assembly.",
    "category": "3D View",
}

import bpy
import os
import time
import shutil
import platform
import subprocess
from datetime import datetime
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import (
    StringProperty, IntProperty,
    EnumProperty
)

# =========================================================
# Global Runtime State
# =========================================================

_RUNNING = False
_TIMER = None
_NEXT_CAPTURE_TIME = 0.0
_LAST_INTERACTION_TIME = 0.0


# =========================================================
# Utilities
# =========================================================

def _timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _resolve_dir(user_dir: str):
    d = user_dir.strip()
    if not d:
        d = "//timelapse"
    return bpy.path.abspath(d)


def _ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def _open_folder(path):
    _ensure_dir(path)
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


# =========================================================
# JPEG Capture
# =========================================================

def _capture_jpeg(path, width, height, quality):
    win, area, region = _find_viewport_region()
    if not win:
        return False

    ctx = bpy.context
    scene = ctx.scene
    r = scene.render
    img = r.image_settings

    old = {
        "filepath": r.filepath,
        "file_format": img.file_format,
        "color_mode": img.color_mode,
        "quality": img.quality,
        "rx": r.resolution_x,
        "ry": r.resolution_y,
        "rp": r.resolution_percentage,
        "ufe": r.use_file_extension,
    }

    r.filepath = path
    r.use_file_extension = True
    img.file_format = 'JPEG'
    img.color_mode = 'RGB'
    img.quality = quality
    r.resolution_x = width
    r.resolution_y = height
    r.resolution_percentage = 100

    ok = True
    try:
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.render.opengl(write_still=True, view_context=True)
    except Exception:
        ok = False

    r.filepath = old["filepath"]
    img.file_format = old["file_format"]
    img.color_mode = old["color_mode"]
    img.quality = old["quality"]
    r.resolution_x = old["rx"]
    r.resolution_y = old["ry"]
    r.resolution_percentage = old["rp"]
    r.use_file_extension = old["ufe"]

    return ok and os.path.exists(path)


# =========================================================
# Properties
# =========================================================

class TL_Props(PropertyGroup):
    output_dir: StringProperty(
        name="Folder",
        subtype="DIR_PATH",
        default=""
    )
    prefix: StringProperty(
        name="Prefix",
        default="snap"
    )
    interval: IntProperty(
        name="Interval (seconds)",
        min=1,
        default=10
    )
    jpeg_quality: IntProperty(
        name="JPEG Quality",
        min=1, max=100,
        default=70
    )
    resolution: EnumProperty(
        name="Resolution",
        items=[
            ('1080p', "1920×1080", ""),
            ('720p', "1280×720", "")
        ],
        default='1080p'
    )
    mp4_fps: IntProperty(
        name="MP4 FPS",
        min=1, max=120,
        default=24
    )
    mp4_quality: EnumProperty(
        name="MP4 Quality",
        items=[
            ('LOW', "Low", ""),
            ('MEDIUM', "Medium", ""),
            ('HIGH', "High", "")
        ],
        default='MEDIUM'
    )


def _props():
    return bpy.context.scene.timelapse_props


# =========================================================
# Pre-start test
# =========================================================

def _test_capture():
    p = _props()
    width, height = _dims(p.resolution)
    temp_path = os.path.join(bpy.app.tempdir, "timelapse_test.jpg")
    ok = _capture_jpeg(temp_path, width, height, p.jpeg_quality)
    exists = os.path.exists(temp_path)
    if exists:
        os.remove(temp_path)
    return ok and exists


# =========================================================
# Start / Stop Operators
# =========================================================

class VIEW3D_OT_timelapse_start(Operator):
    bl_idname = "view3d.timelapse_start"
    bl_label = "Start Screenshots"

    def execute(self, context):
        global _RUNNING, _TIMER, _NEXT_CAPTURE_TIME, _LAST_INTERACTION_TIME

        if not bpy.data.filepath:
            self.report({'ERROR'}, "Save your .blend first.")
            return {'CANCELLED'}

        if _RUNNING:
            return {'CANCELLED'}

        if not _test_capture():
            self.report({'ERROR'}, "Screenshot failed. On macOS: grant Full Disk Access.")
            return {'CANCELLED'}

        p = _props()
        wm = context.window_manager
        _TIMER = wm.event_timer_add(0.1, window=context.window)

        _RUNNING = True
        _NEXT_CAPTURE_TIME = time.time() + float(p.interval)
        _LAST_INTERACTION_TIME = time.time()

        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        global _RUNNING, _TIMER, _NEXT_CAPTURE_TIME, _LAST_INTERACTION_TIME

        if not _RUNNING:
            return {'CANCELLED'}

        now = time.time()

        if event.type != "TIMER":
            _LAST_INTERACTION_TIME = time.time()
            return {'PASS_THROUGH'}
        

        # TIMER event begins ======================================

        p = _props()

        # SAFETY: do not take screenshots during actual renders
        if bpy.app.is_job_running("RENDER"):
            return {'PASS_THROUGH'}

        # 1. Too early → skip
        if now < _NEXT_CAPTURE_TIME:
            return {'PASS_THROUGH'}

        # 2. Skip if user acted recently (0.3 seconds)
        if (now - _LAST_INTERACTION_TIME) < 0.3:
            # Allow only limited skip = interval * 1.5
            if now < (_NEXT_CAPTURE_TIME + p.interval * 1.5):
                return {'PASS_THROUGH'}
            # ELSE → force capture (implicit)

        # 3. Pause if user away too long (interval * 4)
        if (now - _LAST_INTERACTION_TIME) > (p.interval * 4):
            return {'PASS_THROUGH'}

        # 4. Perform capture
        width, height = _dims(p.resolution)
        temp_path = os.path.join(
            bpy.app.tempdir,
            f"{p.prefix}_{_timestamp()}.jpg"
        )

        ok = _capture_jpeg(temp_path, width, height, p.jpeg_quality)

        if ok and os.path.exists(temp_path):
            out = _resolve_dir(p.output_dir)
            _ensure_dir(out)
            final_path = os.path.join(out, os.path.basename(temp_path))
            try:
                os.replace(temp_path, final_path)
            except:
                shutil.copy2(temp_path, final_path)
                os.remove(temp_path)

        # 5. Reset next-capture timer (prevents spam)
        _NEXT_CAPTURE_TIME = now + float(p.interval)

        return {'PASS_THROUGH'}

    def cancel(self, context):
        global _RUNNING, _TIMER
        if _TIMER:
            context.window_manager.event_timer_remove(_TIMER)
        _RUNNING = False


class VIEW3D_OT_timelapse_stop(Operator):
    bl_idname = "view3d.timelapse_stop"
    bl_label = "Stop Screenshots"

    def execute(self, context):
        global _RUNNING, _TIMER
        if _TIMER:
            context.window_manager.event_timer_remove(_TIMER)
        _RUNNING = False
        return {'FINISHED'}



# =========================================================
# MP4 Assembly
# =========================================================

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

    def execute(self, context):
        p = _props()
        width, height = _dims(p.resolution)
        ok = _make_mp4(
            p.output_dir, p.prefix, width, height,
            p.mp4_fps, p.mp4_quality, self.report
        )
        return {'FINISHED'} if ok else {'CANCELLED'}


# =========================================================
# Open Folder
# =========================================================

class VIEW3D_OT_timelapse_open_folder(Operator):
    bl_idname = "view3d.timelapse_open_folder"
    bl_label = "Open Folder"

    def execute(self, context):
        folder = _resolve_dir(_props().output_dir)
        _open_folder(folder)
        return {'FINISHED'}


# =========================================================
# UI Panels
# =========================================================

class VIEW3D_PT_timelapse(Panel):
    bl_label = "Auto Screenshots"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Auto Screenshots"

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        row.scale_y = 1.4

        if _RUNNING:
            row.alert = True
            row.operator("view3d.timelapse_stop", text="Stop", icon="PAUSE")
        else:
            row.operator("view3d.timelapse_start", text="Start", icon="REC")


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
        layout.operator(
            "view3d.timelapse_open_folder",
            text="Open Screenshot Folder",
            icon="FILE_FOLDER"
        )


# =========================================================
# Header Badge
# =========================================================

def _header_badge(self, context):
    if not _RUNNING:
        row = self.layout.row()
        row.alert = True
        row.label(text="NOT RECORDING")


# =========================================================
# Registration
# =========================================================

classes = (
    TL_Props,
    VIEW3D_OT_timelapse_start,
    VIEW3D_OT_timelapse_stop,
    VIEW3D_OT_timelapse_make_mp4,
    VIEW3D_OT_timelapse_open_folder,
    VIEW3D_PT_timelapse,
    VIEW3D_PT_timelapse_options,
)

def _stop_timelapse_for_render(scene):
    """Automatically stop timelapse when any render starts."""
    global _RUNNING, _TIMER

    if not _RUNNING:
        return

    wm = bpy.context.window_manager

    if _TIMER:
        try:
            wm.event_timer_remove(_TIMER)
        except:
            pass
        _TIMER = None

    _RUNNING = False


    

def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.timelapse_props = bpy.props.PointerProperty(type=TL_Props)
    bpy.types.VIEW3D_HT_header.prepend(_header_badge)
    bpy.app.handlers.render_pre.append(_stop_timelapse_for_render)


def unregister():
    bpy.app.handlers.render_pre.remove(_on_render_start)
    bpy.types.VIEW3D_HT_header.remove(_header_badge)
    del bpy.types.Scene.timelapse_props
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

    # NEW: remove render-pre handler if present
    if _stop_timelapse_for_render in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.remove(_stop_timelapse_for_render)

if __name__ == "__main__":
    register()
