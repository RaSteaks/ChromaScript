// Esbuild supplies these virtual modules only to the isolated browser bundle.
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
