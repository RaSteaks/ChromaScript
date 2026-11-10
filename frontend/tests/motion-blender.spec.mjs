import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { compileFloatDocument, loadFloatDocument } from "../motion/runtime.mjs";

const fixtures = JSON.parse(execFileSync("./.venv/bin/python", ["-c", `
import json
from pathlib import Path
from chromascript.motion import MotionSpec
from chromascript.motion_assets import import_gltf
root = Path("tests/fixtures/motion-assets")
spec = MotionSpec(width=320,height=240,frames=25,fps={"numerator":24,"denominator":1},code="export {};")
print(json.dumps({"spec":spec.model_dump(),"display":spec.display(),"assets":{
  kind:import_gltf(root/path) for kind,path in {
    "glb":"blender/character.glb", "gltf":"blender/character.gltf", "unnamed":"triangle.gltf"
  }.items()
}}))
`], { cwd: "..", encoding: "utf8" }));

const code = `
import {Scene, OrthographicCamera, AnimationMixer, AmbientLight, DirectionalLight} from "three";
import {loadGLTF, createGLTFInstance} from "chromascript:assets";
export default async function() {
  const template = await loadGLTF("character");
  const first = createGLTFInstance("character"), second = createGLTFInstance("character");
  first.scene.position.x = -0.85;
  second.scene.position.x = 0.85;
  const mixers = [first,second].map(instance => {
    const mixer = new AnimationMixer(instance.scene);
    mixer.clipAction(instance.animations[0]).play();
    return mixer;
  });
  const scene = new Scene(), camera = new OrthographicCamera(-2.4,2.4,2,-1.6,0.1,20);
  camera.position.set(0,1,6); camera.lookAt(0,1,0);
  scene.add(first.scene,second.scene,new AmbientLight(0xffffff,1));
  const light = new DirectionalLight(0xffffff,2); light.position.set(2,4,6); scene.add(light);
  window.__blender = {template,first,second,scene};
  return {scene,camera,update({time}) {mixers[0].setTime(time);mixers[1].setTime(0);}};
}`;

for (const kind of ["glb", "gltf", "unnamed"]) {
  test(`${kind}: cloned models retain independent animations and deterministic seeks`, async ({ page }, info) => {
    const { html } = await compileFloatDocument({
      spec: { ...fixtures.spec, code, assets: { character: fixtures.assets[kind] } },
      display: fixtures.display,
    }, "frames");
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await loadFloatDocument({
      goto: ({ url, timeout }) => page.goto(url, { timeout }),
      evaluate: (...args) => page.evaluate(...args),
    }, html);
    await page.waitForFunction(() => window.__chromaFloat || window.__chromaError);
    expect(await page.evaluate(() => window.__chromaError)).toBeUndefined();
    // The real Blender export begins at 1/24 s, so its frame-13 peak is 13/24 s.
    const states = await page.evaluate(() => [0,13,6,0].map(frame => {
      const result = window.__chromaFloat.capture(frame);
      const { first, second } = window.__blender;
      const mesh = first.scene.getObjectByProperty("isSkinnedMesh", true);
      const other = second.scene.getObjectByProperty("isSkinnedMesh", true);
      return {
        pixels: window.__chromaFloat.readChunk(0, result.bytes),
        firstMorph: mesh?.morphTargetInfluences.slice(),
        secondMorph: other?.morphTargetInfluences.slice(),
        firstBone: mesh?.skeleton.bones[1].quaternion.toArray(),
        secondBone: other?.skeleton.bones[1].quaternion.toArray(),
      };
    }));
    expect(states[0]).toEqual(states[3]);
    expect(states[0].pixels).not.toBe(states[1].pixels);
    expect(states[1].pixels).not.toBe(states[2].pixels);
    if (kind !== "unnamed") {
      expect(states[1].firstMorph[0]).toBeCloseTo(1);
      expect(states[1].secondMorph[0]).toBe(0);
      expect(states[0].firstBone).not.toEqual(states[1].firstBone);
      expect(states[0].secondBone).toEqual(states[1].secondBone);
      const properties = await page.evaluate(() => {
        const { template, first, second } = window.__blender;
        const mesh = first.scene.getObjectByProperty("isSkinnedMesh", true);
        const other = second.scene.getObjectByProperty("isSkinnedMesh", true);
        const original = template.scene.getObjectByProperty("isSkinnedMesh", true);
        const material = mesh.material;
        const root = first.scene.getObjectByName("ModelRoot");
        return {
          sameSkeleton: mesh.skeleton === other.skeleton,
          sameBones: mesh.skeleton.bones[0] === other.skeleton.bones[0],
          sameMaterial: material === other.material,
          sameGeometry: mesh.geometry === other.geometry,
          sameTexture: material.map === other.material.map,
          sameClip: first.animations[0] === template.animations[0],
          alphaMode: material.transparent, clearcoat: material.clearcoat,
          mapSpace: material.map.colorSpace, normalSpace: material.normalMap.colorSpace,
          roughnessSpace: material.roughnessMap.colorSpace, metalnessSpace: material.metalnessMap.colorSpace,
          flipY: material.map.flipY, originalMorph: original.morphTargetInfluences,
          rootPosition: root.position.toArray(), rootScale: root.scale.toArray(),
          animations: first.animations.map(clip => clip.name),
        };
      });
      expect(properties).toMatchObject({
        sameSkeleton:false, sameBones:false, sameMaterial:false, sameGeometry:true, sameTexture:true,
        sameClip:false, alphaMode:true, mapSpace:"srgb", normalSpace:"", roughnessSpace:"", metalnessSpace:"",
        flipY:false, originalMorph:[0], rootScale:[1.25,1.25,1.25], animations:["BendAndSquash"],
      });
      expect(properties.clearcoat).toBeCloseTo(0.8);
      expect(properties.rootPosition[1]).toBeCloseTo(0.2);
      const disposed = await page.evaluate(() => {
        const { first, second, scene } = window.__blender;
        const mesh = first.scene.getObjectByProperty("isSkinnedMesh", true);
        const other = second.scene.getObjectByProperty("isSkinnedMesh", true);
        const disposed = { material:0, geometry:0, texture:0, otherMaterial:0 };
        mesh.material.addEventListener("dispose", () => disposed.material++);
        mesh.geometry.addEventListener("dispose", () => disposed.geometry++);
        mesh.material.map.addEventListener("dispose", () => disposed.texture++);
        other.material.addEventListener("dispose", () => disposed.otherMaterial++);
        scene.remove(first.scene);
        first.dispose();
        window.__chromaFloat.capture(0);
        return disposed;
      });
      expect(disposed).toEqual({material:1,geometry:0,texture:0,otherMaterial:0});
      await info.attach("blender-properties", { body: JSON.stringify(properties), contentType:"application/json" });
    }
    expect(errors).toEqual([]);
  });
}

test("Blender rig and shape keys render in the production SDR bundle", async ({ page }, info) => {
  const spec = { ...fixtures.spec, engine:"remotion-sdr-v1", assets:{character:fixtures.assets.glb}, code:`
import {useMemo} from "react";
import {AnimationMixer,Box3,Group,PerspectiveCamera,Vector3} from "three";
import {ThreeCanvas} from "@remotion/three";
import {useCurrentFrame,useVideoConfig} from "remotion";
import {createGLTFInstance} from "chromascript:assets";
export default function Animation(){
  const {width,height,fps}=useVideoConfig(); const frame=useCurrentFrame();
  const pair=useMemo(()=>{
    const group=new Group();
    const models=[createGLTFInstance("character"),createGLTFInstance("character")];
    const mixers=models.map((model,index)=>{
      model.scene.position.x=index===0 ? -0.8 : 0.8;
      // Preserve source light intensities; only this material-QA shot uses controlled lighting.
      model.scene.traverse(node=>{if(node.isLight) node.visible=false;});
      group.add(model.scene);
      const mixer=new AnimationMixer(model.scene);mixer.clipAction(model.animations[0]).play();mixer.setTime(0);
      return mixer;
    });
    group.updateMatrixWorld(true);
    const box=new Box3().setFromObject(group,true),size=box.getSize(new Vector3()),center=box.getCenter(new Vector3());
    const camera=new PerspectiveCamera(40,width/height,0.1,100);
    camera.position.copy(center).add(new Vector3(0,0,Math.max(size.y,size.x*height/width)*2.4));camera.lookAt(center);
    return {group,mixers,camera};
  },[width,height]);
  pair.mixers[0].setTime(frame/fps);pair.mixers[1].setTime(0);
  return <ThreeCanvas width={width} height={height} camera={pair.camera}>
  <ambientLight intensity={0.6}/><directionalLight position={[2,4,6]} intensity={2}/>
  <primitive object={pair.group}/>
</ThreeCanvas>;}
` };
  const { serveUrl } = JSON.parse(execFileSync("node",["motion/sdr-runtime.mjs","bundle"],{
    cwd:fileURLToPath(new URL("..",import.meta.url)), input:JSON.stringify({spec,directory:info.outputPath("export")}),
    encoding:"utf8",maxBuffer:8*1024**2,
  }));
  const url = new URL("/blender-export/",process.env.CHROMASCRIPT_URL).href;
  await page.route(url+"**", route => {
    const name = new URL(route.request().url()).pathname.split("/").pop() || "index.html";
    return route.fulfill({body:readFileSync(join(serveUrl,name)),contentType:name.endsWith(".js")?"text/javascript":"text/html"});
  });
  await page.addInitScript(() => {
    window.process={env:{NODE_ENV:"production"}};
    window.remotion_puppeteerTimeout=15000; window.remotion_isMainTab=true;
    window.remotion_inputProps="{}"; window.remotion_initialFrame=0; window.remotion_attempt=1;
    window.remotion_audioEnabled=false; window.remotion_videoEnabled=true; window.remotion_logLevel="error";
  });
  const errors=[];page.on("pageerror",error=>errors.push(error.message));
  await page.goto(url+"index.html");
  await page.waitForFunction(()=>typeof window.remotion_setBundleMode==="function");
  await page.evaluate(()=>window.remotion_setBundleMode({type:"composition",compositionName:"Animation",
    serializedResolvedPropsWithSchema:"{}",compositionDurationInFrames:25,compositionFps:24,compositionWidth:320,compositionHeight:240}));
  const captures=[];
  for (const frame of [0,13,0]) {
    await page.waitForFunction(()=>window.remotion_renderReady);
    await page.evaluate(frame=>window.remotion_setFrame(frame,"Animation",1),frame);
    await page.waitForFunction(()=>window.remotion_renderReady);
    captures.push(await page.locator("canvas").screenshot({path:info.outputPath(`blender-frame-${captures.length}.png`)}));
  }
  expect(captures[0].equals(captures[1])).toBe(false);
  expect(captures[0].equals(captures[2])).toBe(true);
  expect(errors).toEqual([]);
});
