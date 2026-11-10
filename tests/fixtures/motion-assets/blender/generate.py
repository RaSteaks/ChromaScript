"""Rebuild the tiny Blender export fixture with --background --factory-startup.

Run Blender --python this-file -- /absolute/output/directory. Uses only a disposable
scene; no personal .blend, preferences, add-ons or external textures are loaded.
"""

import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

output = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
output.mkdir(parents=True, exist_ok=True)
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
scene.frame_start, scene.frame_end, scene.render.fps = 1, 25, 24
scene.unit_settings.system = "METRIC"

root = bpy.data.objects.new("ModelRoot", None)
scene.collection.objects.link(root)
root.location = (0.1, 0, 0.2)
root.rotation_euler.z = 0.2
root.scale = (1.25, 1.25, 1.25)

mesh = bpy.data.meshes.new("SkinMesh")
mesh.from_pydata([(x, 0, z) for z in (0, 0.8, 1.6) for x in (-0.35, 0.35)], [],
                [(0, 1, 3, 2), (2, 3, 5, 4)])
obj = bpy.data.objects.new("Character", mesh)
scene.collection.objects.link(obj)
uv = mesh.uv_layers.new(name="UVMap")
for polygon in mesh.polygons:
    for loop in polygon.loop_indices:
        vertex = mesh.vertices[mesh.loops[loop].vertex_index].co
        uv.data[loop].uv = ((vertex.x + 0.35) / 0.7, vertex.z / 1.6)

material = bpy.data.materials.new("CeramicGlaze")
material.use_nodes = True
material.blend_method = "BLEND"
obj.data.materials.append(material)
nodes, links = material.node_tree.nodes, material.node_tree.links
principled = nodes.get("Principled BSDF")
principled.inputs["Clearcoat"].default_value = 0.8
principled.inputs["Clearcoat Roughness"].default_value = 0.12
principled.inputs["Emission"].default_value = (0.1, 0.02, 0.01, 1)
principled.inputs["Emission Strength"].default_value = 2

color = bpy.data.images.new("釉色 color", 2, 2, alpha=True)
color.pixels = [component for pixel in [(0.8, 0.15, 0.03, 0.75), (0.05, 0.55, 0.15, 0.75)] * 2 for component in pixel]
texture = nodes.new("ShaderNodeTexImage")
texture.image = color
links.new(texture.outputs["Color"], principled.inputs["Base Color"])
links.new(texture.outputs["Alpha"], principled.inputs["Alpha"])
normal = bpy.data.images.new("Normal", 1, 1)
normal.colorspace_settings.name = "Non-Color"
normal.pixels = (0.5, 0.5, 1, 1)
normal_texture = nodes.new("ShaderNodeTexImage")
normal_texture.image = normal
normal_map = nodes.new("ShaderNodeNormalMap")
links.new(normal_texture.outputs["Color"], normal_map.inputs["Color"])
links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])
orm = bpy.data.images.new("MetallicRoughness", 1, 1)
orm.colorspace_settings.name = "Non-Color"
orm.pixels = (1, 0.42, 0.1, 1)
orm_texture = nodes.new("ShaderNodeTexImage")
orm_texture.image = orm
# This exporter recognizes Separate Color's channel names, not deprecated Separate RGB.
channels = nodes.new("ShaderNodeSeparateColor")
channels.mode = "RGB"
links.new(orm_texture.outputs["Color"], channels.inputs["Color"])
links.new(channels.outputs["Green"], principled.inputs["Roughness"])
links.new(channels.outputs["Blue"], principled.inputs["Metallic"])

obj.shape_key_add(name="Basis")
morph = obj.shape_key_add(name="Squash")
for vertex in morph.data:
    vertex.co.x *= 1.5
for frame, value in ((1, 0), (13, 1), (25, 0)):
    morph.value = value
    morph.keyframe_insert("value", frame=frame)

armature = bpy.data.armatures.new("Rig")
rig = bpy.data.objects.new("Armature", armature)
scene.collection.objects.link(rig)
rig.parent = root
bpy.context.view_layer.objects.active = rig
rig.select_set(True)
bpy.ops.object.mode_set(mode="EDIT")
base = armature.edit_bones.new("RootBone")
base.head, base.tail = (0, 0, 0), (0, 0, 0.8)
tip = armature.edit_bones.new("TipBone")
tip.head, tip.tail, tip.parent, tip.use_connect = (0, 0, 0.8), (0, 0, 1.6), base, True
bpy.ops.object.mode_set(mode="OBJECT")
obj.parent = rig
modifier = obj.modifiers.new("Skin", "ARMATURE")
modifier.object = rig
obj.vertex_groups.new(name="RootBone").add([0, 1, 2, 3], 1, "REPLACE")
obj.vertex_groups.new(name="TipBone").add([4, 5], 1, "REPLACE")
bone = rig.pose.bones["TipBone"]
bone.rotation_mode = "XYZ"
for frame, angle in ((1, 0), (13, math.radians(35)), (25, 0)):
    bone.rotation_euler.z = angle
    bone.keyframe_insert("rotation_euler", frame=frame)

# Matching NLA track names merge skeleton + shape-key channels into one glTF clip.
for animated in (rig, mesh.shape_keys):
    animation = animated.animation_data
    action = animation.action
    track = animation.nla_tracks.new()
    track.name = "BendAndSquash"
    track.strips.new(action.name, 1, action)
    animation.action = None

light = bpy.data.objects.new("KeyLight", bpy.data.lights.new("KeyLight", "POINT"))
scene.collection.objects.link(light)
light.location = (2, -3, 4)
light.data.energy = 80
camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
scene.collection.objects.link(camera)
camera.location = (0, -4, 1.2)
camera.rotation_euler = (Vector((0, 0, 1)) - camera.location).to_track_quat("-Z", "Y").to_euler()
scene.camera = camera
scene.frame_set(1)

settings = dict(export_yup=True, export_animations=True, export_skins=True,
                export_morph=True, export_force_sampling=True, export_nla_strips=True,
                export_apply=False, export_cameras=True, export_lights=True,
                export_draco_mesh_compression_enable=False)
for kind, filename in (("GLB", "character.glb"), ("GLTF_SEPARATE", "character.gltf")):
    bpy.ops.export_scene.gltf(filepath=str(output / filename), export_format=kind,
                              export_texture_dir="textures", **settings)
(output / "provenance.json").write_text(json.dumps({
    "blender_version": bpy.app.version_string,
    "blender_build_hash": bpy.app.build_hash.decode(),
    "settings": settings,
    "fixture": "Original generated geometry and images; no third-party assets.",
}, indent=2) + "\n")
