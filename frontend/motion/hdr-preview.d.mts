/** Typed boundary for the independently testable native MSE controller. */
export type HDRController = {
  readonly enabled: boolean;
  play(): void;
  pause(): void;
  seek(frame: number): void;
  dispose(): void;
};
export function hdrVideoSupport(scope?: Window): boolean;
export function hdrVideoCapabilities(scope?: Window): {
  supported: boolean;
  displayHDR: boolean;
  videoQuerySupported: boolean;
  hevc: boolean;
  reason: string;
};
export function createHDRPreview(options: {
  mount: HTMLElement;
  width: number;
  height: number;
  config: {
    frames: number;
    fps: number;
    frameRate: { numerator: number; denominator: number };
    display: { working_space: { source: string } };
  };
  capture(frames: number[], signal: AbortSignal): Promise<ArrayBuffer[]>;
  encodeChunk(header: unknown, frames: ArrayBuffer[]): Promise<unknown>;
  onFrame(frame: number): void;
  onPlaying(playing: boolean): void;
  onStatus(status: { mode: "sdr" | "hdr" | "buffering"; message: string }): void;
  onFallback(): void;
}): HDRController;
