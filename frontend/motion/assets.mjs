// This trusted module is bundled into the opaque browser document, never run in Node.
import { LoadingManager } from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { clone } from "three/addons/utils/SkeletonUtils.js";
import documents from "chromascript:asset-data";

const loaded = new Map();
const ready = new Map();
export function loadGLTF(name) {
  if (!Object.hasOwn(documents, name))
    return Promise.reject(new Error(`找不到 glTF 资源：${name}`));
  if (!loaded.has(name)) {
    loaded.set(name, (async () => {
      const failures = [];
      const manager = new LoadingManager();
      // Even a direct compiler caller cannot turn an asset into network or file I/O.
      manager.setURLModifier((url) => {
        if (!url.startsWith("data:") && !url.startsWith("blob:"))
          throw new Error("glTF 只能加载修订内嵌资源；请重新导入模型及其依赖。");
        return url;
      });
      manager.onError = () => failures.push("无法解码 glTF 贴图或缓冲区");
      const result = await new GLTFLoader(manager).parseAsync(documents[name], "");
      // GLTFLoader can recover a failed texture as null; never certify that as a
      // complete imported model or render a misleading untextured first frame.
      if (failures.length) throw new Error(`${name}: ${failures[0]}`);
      ready.set(name, result);
      return result;
    })());
  }
  return loaded.get(name);
}

// SDR mounts only after prepareAssets(), so components can read models without
// an empty first commit or their own asynchronous loading/delayRender plumbing.
export function getGLTF(name) {
  if (!ready.has(name)) throw new Error(`glTF 资源尚未加载：${name}`);
  return ready.get(name);
}

// Keep the cached template immutable across shots. Object3D.clone() alone leaves
// SkinnedMesh bones bound to the original rig; SkeletonUtils reconnects each copy.
export function createGLTFInstance(name) {
  const template = getGLTF(name);
  const scene = clone(template.scene);
  const materials = new Map();
  const skeletons = new Set();
  const originals = [];
  const nodeIds = new Map();
  template.scene.traverse((node) => originals.push(node.uuid));
  let index = 0;
  scene.traverse((node) => {
    nodeIds.set(originals[index++], node.uuid);
    if (node.isSkinnedMesh) skeletons.add(node.skeleton);
    if (!node.material) return;
    const independent = (material) => {
      if (!materials.has(material)) materials.set(material, material.clone());
      return materials.get(material);
    };
    node.material = Array.isArray(node.material)
      ? node.material.map(independent) : independent(node.material);
  });
  const animations = template.animations.map((clip) => {
    const copy = clip.clone();
    for (const track of copy.tracks) {
      // GLTFLoader binds unnamed nodes by UUID; cloned nodes get new UUIDs.
      // Blender names normally survive unchanged, including morph-weight tracks.
      const dot = track.name.indexOf(".");
      const target = nodeIds.get(track.name.slice(0, dot));
      if (target) track.name = target + track.name.slice(dot);
    }
    return copy;
  });
  return {
    scene, animations,
    dispose() {
      // Geometry and textures are shared read-only with the template/other shots.
      // Dispose only instance-owned resources; never invalidate another instance.
      for (const material of materials.values()) material.dispose();
      for (const skeleton of skeletons) skeleton.dispose();
    },
  };
}

export async function prepareAssets() {
  await Promise.all(Object.keys(documents).map(loadGLTF));
}
