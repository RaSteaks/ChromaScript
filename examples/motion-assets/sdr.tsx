import { useEffect, useMemo } from "react";
import { AnimationMixer } from "three";
import { ThreeCanvas } from "@remotion/three";
import { useCurrentFrame, useVideoConfig } from "remotion";
import { createGLTFInstance } from "chromascript:assets";

export default function Animation() {
  const frame = useCurrentFrame();
  const config = useVideoConfig();
  // The host preloads geometry and textures before mounting the composition.
  // Each mounted shot owns its bones, morph weights, material values and clips.
  const model = useMemo(() => createGLTFInstance("horse"), []);
  const mixer = useMemo(() => {
    const value = new AnimationMixer(model.scene);
    if (model.animations[0]) value.clipAction(model.animations[0]).play();
    return value;
  }, [model]);
  useEffect(() => () => {
    mixer.stopAllAction();
    mixer.uncacheRoot(model.scene);
    model.dispose();
  }, [mixer, model]);
  // Absolute time also works when seeking backwards or sampling fractional frames.
  mixer.setTime(frame / config.fps);
  return <ThreeCanvas width={config.width} height={config.height} camera={{ position: [0, 1, 5], fov: 45 }}>
    <ambientLight intensity={1} />
    <directionalLight position={[3, 4, 5]} intensity={3} />
    <primitive object={model.scene} />
  </ThreeCanvas>;
}
