# Blender export fixture

Original test geometry and generated images, exported by Blender 3.6.10 with its
bundled Khronos glTF Blender I/O v3.6.28. No downloaded or third-party models.
`provenance.json` records the build and export options. `character.glb` and
`character.gltf` (with `character.bin` and `textures/`) represent the same scene.

Reproduce from the repository root with Blender 3.6.10:

```sh
/Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
  --python tests/fixtures/motion-assets/blender/generate.py \
  -- tests/fixtures/motion-assets/blender
```

The script replaces only its generated fixture files. It uses an empty disposable
scene, never reads a personal `.blend`, and does not save preferences. Normal tests
consume the checked-in exports and do not require Blender. The script uses Blender
3.6's Principled BSDF input names; newer Blender versions need corresponding names.

Coverage: two-bone skin, a `Squash` morph, a named `BendAndSquash` NLA animation,
nonzero first keyframe (1/24 s), parent translation/rotation/scale, PBR base color
and alpha, normal and metallic/roughness maps, clearcoat, Unicode/space image paths,
a point light and a camera. Metallic/roughness packing uses Separate Color, with
actual exported G=107 and B=26 for authored .42/.1. Light intensity is preserved;
the browser material-QA shot disables imported lights and supplies controlled ones.
