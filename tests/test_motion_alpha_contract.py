"""Real float GPU output must obey material coverage before CPU unassociation."""

import numpy as np
import pytest

from chromascript.errors import ChromaError
from chromascript.motion import MotionSpec, motion_environment
from chromascript.motion_color import decode_float_frame
from chromascript.motion_render import render_float_frames

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not motion_environment()["render_available"], reason="local Chrome/npm/FFmpeg required"
    ),
]


def capture(tmp_path, code, frames=1):
    pixels = []

    def accept(index, path):
        pixels.append(decode_float_frame(path.read_bytes(), 16, 16))

    render_float_frames(
        MotionSpec(width=16, height=16, frames=frames, code=code),
        tmp_path,
        list(range(frames)),
        accept,
    )
    return pixels


def scene_code(options="", fragment="gl_FragColor=vec4(2.,2.,2.,0.5);", update=""):
    return f'''import * as THREE from "three";
export default function createScene() {{
 const scene=new THREE.Scene(),camera=new THREE.Camera();
 const material=new THREE.ShaderMaterial({{{options} depthTest:false,
 vertexShader:`void main(){{gl_Position=vec4(position,1.0);}}`,
 fragmentShader:`void main(){{{fragment}}}`}});
 scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),material));
 return {{scene,camera,update({{frame}}){{{update}}}}};
}}'''


@pytest.mark.parametrize("ending", ["", "return;"])
def test_opaque_shader_forces_coverage_without_changing_radiance(tmp_path, ending):
    # Shader alpha is not Material.opacity. Check actual output, including an
    # early return that would bypass an assignment inserted at the end of main.
    image = capture(tmp_path, scene_code(fragment=f"gl_FragColor=vec4(2.,2.,2.,0.5);{ending}"))[0]
    np.testing.assert_allclose(image, np.broadcast_to([2, 2, 2, 1], image.shape), atol=2e-6)


def test_opaque_shader_keeps_discarded_background_transparent(tmp_path):
    image = capture(
        tmp_path,
        scene_code(fragment="if(gl_FragCoord.x<8.)discard;gl_FragColor=vec4(2.,2.,2.,0.5);"),
    )[0]
    np.testing.assert_array_equal(image[:, :8], 0)
    np.testing.assert_allclose(
        image[:, 8:], np.broadcast_to([2, 2, 2, 1], image[:, 8:].shape), atol=2e-6
    )


def test_transparency_switch_recompiles_and_preserves_authored_shader(tmp_path):
    # Opaque/transparent sorting remains owned by Three. Updating coverage must
    # invalidate the compiled guard without rewriting the user's shader string.
    code = scene_code(update='''
 if(!material.fragmentShader.includes("vec4(2.,2.,2.,0.5)") ||
    material.fragmentShader.includes("chromascriptOpaqueMain"))throw new Error("source changed");
 material.transparent=frame===1;''')
    images = capture(tmp_path, code, frames=3)
    for frame, image in enumerate(images):
        expected = [2, 2, 2, 0.5 if frame == 1 else 1]
        np.testing.assert_allclose(image, np.broadcast_to(expected, image.shape), atol=2e-6)


@pytest.mark.parametrize("raw", [False, True])
def test_glsl3_opaque_shader_preserves_named_output_and_compile_hook(tmp_path, raw):
    # A custom onBeforeCompile runs first; its RGB edits and cache key survive
    # the alpha guard for both ShaderMaterial and RawShaderMaterial.
    material_type = "RawShaderMaterial" if raw else "ShaderMaterial"
    declarations = "in vec3 position;" if raw else ""
    code = f'''import * as THREE from "three";
export default function createScene() {{
 const scene=new THREE.Scene(),camera=new THREE.Camera();
 const material=new THREE.{material_type}({{glslVersion:THREE.GLSL3,depthTest:false,
 vertexShader:`precision highp float;{declarations}void main(){{gl_Position=vec4(position,1.0);}}`,
 fragmentShader:`precision highp float;layout(location=0)out highp vec4 color;
 void assign(out vec4 value);
 void assign(out vec4 value){{value=vec4(2.,2.,2.,0.5);}}
 void main(){{assign(color);return;}}`}});
 material.onBeforeCompile=(shader)=>{{shader.fragmentShader=shader.fragmentShader.replace("2.,2.,2.","3.,3.,3.");}};
 material.customProgramCacheKey=()=>"authored-key";
 scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),material));
 return {{scene,camera,update(){{}}}};
}}'''
    image = capture(tmp_path, code)[0]
    np.testing.assert_allclose(image, np.broadcast_to([3, 3, 3, 1], image.shape), atol=2e-6)


def test_transparent_no_blending_is_rejected_before_unassociation(tmp_path):
    with pytest.raises(ChromaError, match="NormalBlending"):
        capture(tmp_path, scene_code(options="transparent:true,blending:THREE.NoBlending,"))


def test_declared_premultiplied_shader_preserves_associated_radiance(tmp_path):
    # With premultipliedAlpha=true, Three's source RGB factor is ONE. The shader
    # therefore supplies 2 * 0.5 itself; CPU readback unassociates exactly once.
    image = capture(
        tmp_path,
        scene_code(
            options="transparent:true,premultipliedAlpha:true,",
            fragment="gl_FragColor=vec4(1.,1.,1.,0.5);",
        ),
    )[0]
    np.testing.assert_allclose(image, np.broadcast_to([2, 2, 2, 0.5], image.shape), atol=2e-6)


def test_ambiguous_opaque_glsl3_outputs_fail_closed(tmp_path):
    code = scene_code(options="glslVersion:THREE.GLSL3,")
    code = code.replace(
        "fragmentShader:`void main(){gl_FragColor=vec4(2.,2.,2.,0.5);}`",
        "fragmentShader:`layout(location=0)out vec4 first;layout(location=1)out vec4 second;"
        "void main(){first=vec4(2.,2.,2.,0.5);second=first;}`",
    )
    with pytest.raises(ChromaError, match="唯一 vec4"):
        capture(tmp_path, code)
