// Test-only WebKit transport. Source, Freeze wrapper, sampling/batching and PNG
// integration are production code; only Remotion's Chromium screenshot driver is replaced.
import { webkit } from "@playwright/test";
import { build } from "esbuild";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { sourceModules } from "../motion/source-modules.mjs";
import { sampledScene, renderSampledFrames } from "../motion/sdr-sampling.mjs";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
const spec = request.spec;
const root = fileURLToPath(new URL("..", import.meta.url));
const compiled = await build({
  stdin: {
    contents: `
    import React from 'react'; import {createRoot} from 'react-dom/client';
    import {Player} from '@remotion/player';
    import {Freeze,useCurrentFrame} from 'remotion';
    import Scene from 'chromascript:scene';
    ${sampledScene}
    const root=createRoot(document.getElementById('root'));
    let generation=0;
    window.renderSample=(frame,sampleFrames)=>new Promise(resolve=>{
      root.render(<Player key={++generation} component={SampledScene}
        inputProps={{sampleFrames}} initialFrame={frame}
        compositionWidth={${spec.width}} compositionHeight={${spec.height}}
        durationInFrames={${spec.frames}} fps={${spec.fps.numerator / spec.fps.denominator}}
        initiallyMuted errorFallback={({error})=>{window.sampleError=error.message;return null;}} style={{width:'100%',height:'100%'}} />);
      requestAnimationFrame(()=>requestAnimationFrame(()=>setTimeout(resolve,30)));
    });`,
    loader: "tsx",
    resolveDir: root,
  },
  bundle: true,
  write: false,
  format: "iife",
  platform: "browser",
  jsx: "automatic",
  define: { "process.env.NODE_ENV": '"production"' },
  plugins: [
    sourceModules(
      spec,
      new Set([
        "react",
        "react/jsx-runtime",
        "remotion",
        "@remotion/three",
        "@react-three/fiber",
        "three",
      ]),
      root,
    ),
  ],
});
const browser = await webkit.launch({ headless: true });
try {
  const scale = request.scale ?? 1;
  const page = await browser.newPage({
    viewport: { width: spec.width * scale, height: spec.height * scale },
  });
  await page.route("**/*", (route) => route.abort());
  await page.setContent(
    '<style>html,body,#root{margin:0;width:100%;height:100%;overflow:hidden}*{box-sizing:border-box}</style><div id="root"></div>',
  );
  await page.addScriptTag({ content: compiled.outputFiles[0].text });
  const capture = async (options) => {
    mkdirSync(options.outputDir, { recursive: true });
    for (const frame of options.frames) {
      await page.evaluate(
        ({ frame, samples }) => window.renderSample(frame, samples),
        { frame, samples: options.composition.props.sampleFrames },
      );
      const error = await page.evaluate(() => window.sampleError);
      if (error) throw new Error(error);
      await page.screenshot({
        path: join(
          options.outputDir,
          options.imageSequencePattern.replace("[frame]", String(frame)),
        ),
        omitBackground: true,
      });
    }
  };
  await renderSampledFrames(
    capture,
    {
      composition: { props: {} },
      onFrameUpdate: (completed) =>
        process.stdout.write(JSON.stringify({ completed }) + "\n"),
    },
    request,
  );
} finally {
  await browser.close();
}
