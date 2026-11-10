// Esbuild supplies these virtual modules only to the isolated browser bundle.
declare module "chromascript:assets" {
  import type { GLTF } from "three/addons/loaders/GLTFLoader.js";
  import type { AnimationClip, Group } from "three";
  export function loadGLTF(name: string): Promise<GLTF>;
  export function getGLTF(name: string): GLTF;
  export function createGLTFInstance(name: string): {
    scene: Group;
    animations: AnimationClip[];
    dispose(): void;
  };
  export function prepareAssets(): Promise<void>;
}
declare module "chromascript:scene" {
  import type { ComponentType } from "react";
  const Scene: ComponentType;
  export default Scene;
}
declare module "chromascript:config" {
  const config: {
    width: number;
    height: number;
    frames: number;
    fps: number;
    channel: string;
  };
  export default config;
}
