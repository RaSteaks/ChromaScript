"""SDR React/Remotion templates remain distinct from scene-linear float factories."""

from .motion import MotionSpec


def sdr_templates() -> dict[str, MotionSpec]:
    return {
        "title": MotionSpec(
            engine="remotion-sdr-v1",
            name="SDR 文字与图形",
            code="""import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from "remotion";

export default function Animation() {
  const frame = useCurrentFrame();
  const {fps, width} = useVideoConfig();
  // Frame-based time makes seeking, preview and offline rendering agree.
  const enter = spring({frame, fps, config: {damping: 18}});
  const slide = interpolate(enter, [0, 1], [80, 0]);
  return (
    <AbsoluteFill style={{backgroundColor: "#172b3a", color: "#e7f0f4", justifyContent: "center", padding: width * 0.08, fontFamily: '"PingFang SC", sans-serif'}}>
      <div style={{opacity: enter, transform: `translateY(${slide}px)`}}>
        <div style={{fontSize: width * 0.018, letterSpacing: "0.2em", color: "#8cc8d2"}}>CHROMASCRIPT / MOTION</div>
        <h1 style={{fontSize: width * 0.075, margin: "24px 0", fontWeight: 600}}>让代码动起来</h1>
        <div style={{height: 5, width: width * 0.32 * enter, backgroundColor: "#8cc8d2"}} />
        <p style={{fontSize: width * 0.023, opacity: 0.8}}>编辑左侧代码，即时预览你的下一帧。</p>
      </div>
    </AbsoluteFill>
  );
}
""",
        ),
        "sdr-three": MotionSpec(
            engine="remotion-sdr-v1",
            name="SDR 三维旋转几何",
            code="""import {AbsoluteFill, useCurrentFrame, useVideoConfig} from "remotion";
import {ThreeCanvas} from "@remotion/three";

export default function Animation() {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  // Never accumulate rotation with useFrame/delta: every frame is independent.
  const time = frame / fps;
  return (
    <AbsoluteFill style={{backgroundColor: "#172b3a"}}>
      <ThreeCanvas width={width} height={height} camera={{position: [0, 0, 5], fov: 42}} gl={{antialias: true}}>
        <ambientLight intensity={0.7} />
        <directionalLight position={[3, 4, 5]} intensity={3} />
        <directionalLight position={[-3, -2, 2]} intensity={1.5} color="#b3dde8" />
        <mesh rotation={[time * 0.35, time * 0.6, 0.15]}>
          <torusKnotGeometry args={[0.85, 0.27, 128, 24]} />
          <meshStandardMaterial color="#88bbc8" metalness={0.45} roughness={0.28} />
        </mesh>
      </ThreeCanvas>
      <div style={{position: "absolute", left: "7%", bottom: "8%", color: "#e7f0f4", fontFamily: '"PingFang SC", sans-serif', fontSize: width * 0.027}}>Three.js · 每一帧由代码定义</div>
    </AbsoluteFill>
  );
}
""",
        ),
    }
