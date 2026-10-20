import * as THREE from "three";
import config from "chromascript:config";

// This module must execute BEFORE the user's module (including top-level Colors).
// Numeric Color/shader values, lighting and RGBA32F storage share one linear basis.
const contract = config.display.working_space;
export const sourceSpace = contract.source;
export function registerSpace(contract) {
  const name =
    contract.source === "Linear Rec.709 (sRGB)"
      ? THREE.LinearSRGBColorSpace
      : `chromascript:${contract.source}`;
  if (name === THREE.LinearSRGBColorSpace) return name;
  const rec709 = THREE.ColorManagement.spaces[THREE.LinearSRGBColorSpace];
  // Three's conversion graph uses a common XYZ reference. Adapt AP1 D60 to that
  // D65 reference with the OCIO-owned matrix; never insert a display transfer.
  const to709 = new THREE.Matrix3().set(...contract.to_linear_rec709.flat());
  const toXYZ = rec709.toXYZ.clone().multiply(to709);
  const fromXYZ = toXYZ.clone().invert();
  THREE.ColorManagement.define({
    [name]: {
      primaries: contract.primaries,
      whitePoint: contract.white_point,
      transfer: THREE.LinearTransfer,
      toXYZ,
      fromXYZ,
      luminanceCoefficients: [
        toXYZ.elements[1],
        toXYZ.elements[4],
        toXYZ.elements[7],
      ],
      workingColorSpaceConfig: { unpackColorSpace: THREE.SRGBColorSpace },
      outputColorSpaceConfig: { drawingBufferColorSpace: THREE.SRGBColorSpace },
    },
  });
  return name;
}
export const workingColorSpace = registerSpace(contract);
THREE.ColorManagement.enabled = true;
THREE.ColorManagement.workingColorSpace = workingColorSpace;
export function assertWorkingSpace() {
  if (
    !THREE.ColorManagement.enabled ||
    THREE.ColorManagement.workingColorSpace !== workingColorSpace
  )
    throw new Error(
      "请保留所选源码线性工作空间；需要更改时请修改动画的源码颜色空间。",
    );
}
