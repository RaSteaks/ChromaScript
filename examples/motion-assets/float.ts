import { AnimationMixer, AmbientLight, DirectionalLight, PerspectiveCamera, Scene } from "three";
import { createGLTFInstance, loadGLTF } from "chromascript:assets";

// Set source_color_space to "Linear Rec.709 (sRGB)" for imported glTF colors.
export default async function createScene({ width, height }: { width: number; height: number }) {
  await loadGLTF("horse");
  const model = createGLTFInstance("horse");
  const scene = new Scene();
  const camera = new PerspectiveCamera(45, width / height, 0.1, 100);
  camera.position.set(0, 1, 5);
  const light = new DirectionalLight(0xffffff, 3);
  light.position.set(3, 4, 5);
  scene.add(model.scene, new AmbientLight(0xffffff, 1), light);
  const mixer = new AnimationMixer(model.scene);
  if (model.animations[0]) mixer.clipAction(model.animations[0]).play();
  return { scene, camera, update({ time }: { time: number }) {
    // Absolute time preserves deterministic seeks and rational-rate sampling.
    mixer.setTime(time);
  } };
}
