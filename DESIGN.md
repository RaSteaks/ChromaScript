---
{
  "version": "alpha",
  "colors": {
    "background": "#191B1E",
    "surface": "#22252A",
    "raised": "#2B2F35",
    "border": "#3B4149",
    "text": "#E3E6EB",
    "muted": "#A9B1BC",
    "success": "#91C7A5",
    "warning": "#E6C184",
    "danger": "#F1A3A3",
    "focus": "#B3DDE8",
    "scrollbar": "#66717F",
    "primary": "#88BBC8",
    "pickerLight": "#FFFFFF",
    "pickerDark": "#000000",
    "previewBackground": "#343434",
    "previewChecker": "#353535"
  },
  "typography": {
    "sans": {
      "fontFamily": "\"PingFang SC\", \"Microsoft YaHei\", system-ui, sans-serif",
      "fontSize": "14px",
      "lineHeight": "1.6"
    },
    "mono": {
      "fontFamily": "\"SFMono-Regular\", Consolas, \"Liberation Mono\", monospace",
      "fontSize": "12px",
      "lineHeight": "1.6"
    }
  },
  "rounded": {
    "DEFAULT": "6px",
    "panel": "8px"
  },
  "spacing": {
    "xs": "4px",
    "sm": "8px",
    "md": "12px",
    "lg": "20px",
    "xl": "28px"
  },
  "components": {
    "button": {
      "height": "34px",
      "backgroundColor": "{colors.raised}",
      "textColor": "{colors.text}",
      "typography": "{typography.sans}",
      "rounded": "{rounded.DEFAULT}"
    },
    "primaryButton": {
      "backgroundColor": "{colors.primary}",
      "textColor": "{colors.background}"
    },
    "input": {
      "height": "34px",
      "backgroundColor": "{colors.surface}",
      "textColor": "{colors.text}"
    },
    "hint": {
      "textColor": "{colors.muted}"
    },
    "separator": {
      "backgroundColor": "{colors.border}"
    },
    "success": {
      "textColor": "{colors.success}"
    },
    "warning": {
      "textColor": "{colors.warning}"
    },
    "error": {
      "textColor": "{colors.danger}"
    },
    "focusRing": {
      "backgroundColor": "{colors.focus}"
    },
    "scrollbarThumb": {
      "backgroundColor": "{colors.scrollbar}"
    },
    "preview": {
      "backgroundColor": "{colors.previewBackground}",
      "rounded": "{rounded.panel}"
    },
    "previewCheckerboard": {
      "backgroundColor": "{colors.previewChecker}"
    },
    "colorPickerMarker": {
      "backgroundColor": "{colors.pickerDark}",
      "textColor": "{colors.pickerLight}"
    }
  }
}
---
# ChromaScript

## Overview
A professional Chinese procedural-image workstation, informed by a color-grading suite and a shader laboratory. The image is the primary surface. Its signature is the original ACES pixel readout beside a visibly separate display preview. Avoid marketing cards, decorative charts, and saturated chrome around the picture.

## Colors
The JSON-compatible YAML frontmatter is the token source of truth. scripts/design_tokens.py generates frontend/src/tokens.css and verifies drift with --check. Neutral gray surrounds the preview; the restrained cyan accent identifies actions and focus. Status always includes text.

## Typography
Use local Chinese system fonts and fixed-width numerals/code; no remote font requests. Body 14px and utility 12px. Keep technical units visible.

## Layout
A four-stage strip is the workflow signature: choose a scene → edit and preview → configure delivery → jobs and artifacts. The selected project, scene and save state share one line above it. On wide windows each stage is a single line with its name on the left and live state on the right; from 1200px down the state stacks under the name. Stage numbers express real order; stages are freely revisitable views, not a locked wizard. The strip already names the stage, so the stage heading is a compact row: heading, one-line instruction and the stage action.

Desktop: scene library uses a 340px project-and-scene column, whose scene rows show their last-saved Asia/Taipei time, and a template gallery of text-sized cards that auto-fills columns of at least 250px. Editing uses a left column containing the large preview and its code disclosure, with a 300px inspector on the right that starts with scene parameters, so they are tuned beside the picture, followed by editable canvas/time settings and the read-only color workflow. One row above the picture holds the view switch (live, keyframes, motion) on the left and the stored-render actions for the saved revision on the right; guidance and the display label sit on a caption line beneath it. Code aligns with both edges of the preview and follows its pixel readout with `spacing.lg` (`--space-lg`); inspector height never inserts blank space above the editor. The editor toolbar wraps within this column. Delivery pairs a 320px read-only shared-specification column, sticky on tall desktop windows, with a wide format editor and a review/action bar. Jobs pair a paginated queue with a separate results panel; the selected task row is raised with a primary edge, and elapsed time never wraps. Every top-level page, including MCP, shares the workbench frame bounded at 1600px for legibility. At 900px stack preview, code and inspector in that order; at 560px the workflow strip reflows into two columns. The preview reserves a 16:9 media surface, caps its height on short laptop windows and contains the original image without cropping. Document scrolling owns all long forms; no viewport clipping or nested form scroller. The preview reserves a 16:9 media surface, caps its height on short laptop windows and contains the original image without cropping. Actions and errors wrap without widening the page.

## Elevation & Depth
Flat panels with fine borders. Modal dialogs alone use a restrained shadow and an inert backdrop.

## Shapes
6px controls and 8px preview frames. No ornamental pill cards.

## Components
MotionStorageEstimate owns the read-only disk budget inside animation export, below DeliveryDestination. Reuse section-heading, Button, Notice, specification-summary and the native technical-note disclosure. Show uncompressed EXR size separately from the destination budget and observed free space; separate volumes show workspace space independently. Existing palette/type/spacing tokens remain unchanged; regenerate/check tokens.css together. Keep loading/status geometry reserved, wrap narrow summary values, and never present compressed size as known.

ProjectAudio owns one shared “项目音频” dialog for native and animation workspaces. Reuse Button, Field, Modal, Notice and native selects/file/audio controls. Two-column timing/gain fields collapse to one below 560px; bounded asset pages show five native audio players, without autoplay. The existing dark neutral palette, spacing, mono/sans typography and focus roles remain unchanged. Regenerate/check tokens.css from the unchanged frontmatter. Timeline editing is explicit and revision checked, separate from source audition and silent visual previews.

MotionWorkbench owns the independent “动画代码” workspace. Its project and animation pickers keep readable widths, with outlined template actions at the end of the same row; when a project is loaded and nothing is open, a dashed empty state points to those controls without duplicating them. Desktop uses a TypeScript editor on the left and a fixed media surface, Chinese playback controls and canvas/time settings on the right; below 900px these stack in that order. The image and syntax are the expressive surfaces; chrome retains the existing neutral palette, local type, spacing and shared button states. CodeEditor adapts generated background/surface/raised/text/muted/focus/border roles and typography.mono to locally bundled CodeMirror; Tab leaves the editor. No new palette tokens are introduced: regenerate and check tokens.css from the unchanged frontmatter.

MotionPreview owns sandboxed Three.js RGBA32F candidates and the last working picture. HDR three-float-v1 factories coexist with SDR remotion-sdr-v1 React/DOM/ThreeCanvas compositions. A primary “新建空白动画” action and native engine selector replace the example-button wall. Examples remain under a native “示例模板（可选）” disclosure, named by discovery. The WGSL library places “新建空白场景” before its optional example grid. Both blank actions reuse existing Button/Field tokens; no new visual values. Historical SDR records open without rewriting revisions. Compile/runtime/float-capability errors retain the last picture; hidden views pause playback. A generated shader from the pinned OCIO ACES SDR transform produces sRGB display copies without altering the float target. MotionWorkbench's export area reuses Field/native selects for EXR Half/Float and HDR PQ/HLG or SDR sRGB/Rec.709 video; discovery owns options and unavailable encoders. Delivery selection never changes preview colors. Engine-specific discovery hides EXR fields for SDR source and resets incompatible outputs on source changes; all sources retain playback and draft/recovery controls. Result actions expose the master ZIP, HDR video and verification report, with a separately labeled sRGB thumbnail. DeliveryDestination's animation variant describes source/EXR/HDR bundles. All visual roles use existing generated tokens; no new palette or spacing values. Drafts, revision conflicts and jobs survive navigation.

Animation discovery reserves the existing status line for loading feedback and uses Notice for actionable service/retry errors. Keep successfully loaded projects visible during partial discovery failure; show the project-creation empty state only after a confirmed empty project response. These recovery states reuse the existing controls and generated tokens, without introducing new visual values.

External animation saves update the current editor automatically through revision reads; no additional refresh control is introduced. The shared status line acknowledges an accepted external revision without moving focus. Notice beside the code actions owns persistent draft-conflict and synchronization-error feedback; copy/reload and the existing connection control retain their established Button/Modal behavior. Updates preserve the editor instance and preview surface geometry. Reuse the existing semantic, spacing and typography tokens; regenerate and check tokens.css from the unchanged frontmatter.

ColorWorkflow owns the shared read-only color explanation beside preview settings and delivery specifications. Code generation and internal processing are fixed linear ACEScg/AP1; the browser always uses the ACES 2.0 sRGB view, independently of user-selected delivery gamut/gamma. Reuse the section-heading, frame-settings, specification-summary and technical-note patterns with existing generated spacing/type tokens. Parameter storage labels explicitly describe parameter RGB only; the shader receives ACEScg. No input/work-space dropdown is introduced. This copy-only extension adds no visual tokens; regenerate and check tokens.css against the unchanged frontmatter.

The preview opens in live draft mode. Its inspector owns the full canvas specification form, below the scene parameters: size preset, width, height, rational frame rate, frame count, final-render sampling and start timecode. Delivery reuses those controlled values in a read-only summary with a return-to-preview action. Both sections reuse Field, Button and the generated spacing, border and typography tokens; only one canvas form exists. Its segmented control shares Button states with stored keyframes and motion review. Play/pause, a native keyboard-operable timeline and the current-frame input sit below the existing fixed media surface. Low-resolution dimensions, single sampling and stale/error feedback remain visible in a reserved status row. Live images share the same contained geometry and alpha checkerboard as stored previews; `colors.previewBackground` and `colors.previewChecker` generate the shared media tokens. Original pixel readouts remain unavailable for transient proxies; the readout lays R, G, B and A out as one row with each value beside its letter. No animation starts without Play, and hidden views stop preview work. Stored motion preview accepts only verified sRGB video; the unavailable control explains that legacy or other-Gamma video needs a newly generated sRGB preview. Reuse the existing Button and title pattern without new tokens.

ColorField owns scene color selection through the shared Modal, Field, Button and Notice. Its primary control is an ACES-rendered color swatch opening an OKLCH lightness/chroma plane and native hue/lightness/chroma sliders. Hatched areas mark colors beyond sRGB; text explains chroma reduction at fixed lightness and hue. Original/new swatches are separate from stored RGB, and the RGB editor is a native advanced disclosure. The palette is the only expressive color surface. `colorPickerMarker` adapts its light foreground and dark background roles to a ring and outer outline, leaving the selected color visible through the center. `colors.pickerLight` / `colors.pickerDark` generate `--color-pickerLight` / `--color-pickerDark` for these roles; all other picker chrome reuses existing semantic and spacing tokens.

Shared Button, Field, Textarea, Modal, Notice and Search own their states. WorkflowNav owns the named stage strip; active stage uses an underline and explicit aria-current. Each stage has one heading, concise instruction, and a clear next action. Delivery review shows actual values; only a successful submission navigates to jobs. Code disclosure keeps expert controls available without competing with the first preview. DeliveryEditor owns each user-defined output target and reuses native selects through Field. The core delivery capability matrix owns dropdown options, compatibility and normalization; no preset is selected in the workbench. Native selects intentionally retain OS popup ownership. Buttons preserve dimensions while pending. Code editing remains available without a GPU preview. Scrollbar tokens apply globally. Visible keyboard focus and reduced-motion support are required.


Master-first animation delivery: new blank animations use the float engine; SDR remains explicitly selectable. Final output defaults to ACEScg Float32 ZIP masters only and 2×2 spatial supersampling, with 1×1 and 4×4 alternatives; the low-resolution preview stays single-sampled. MotionWorkbench owns editable positive-integer custom dimensions/frame count and a rational numerator/denominator text field alongside rate presets. GPU, memory and codec limitations fail explicitly. Use existing Field, Button, Modal and Notice tokens; no new visual tokens. Regenerate/check tokens.css from the unchanged frontmatter.

MotionWorkbench distinguishes measured float-pipeline verification from revision-bound source-color review. An optional shared Modal records reviewer and evidence against the revision captured when opened; conflicts retain form text, and cancellation changes no review. A saved review is an explicit attestation, not automatic shader certification. Unreviewed shaders remain usable. Dirty/new revisions show unreviewed; job results show the frozen review and independent native video tag verification.

Both animation engines stage intermediate frames, bundler outputs, encoder files and final output on the selected export volume. Workspace keeps task metadata/logs; system browser temporary profiles may remain local. Missing/replaced volume fails rather than falling back locally. DeliveryDestination explains direct external-disk writing. Master-mode float recovery retains verified EXRs; video-only and SDR rerender on retry.

Mandatory exact video-tag policy supersedes the earlier MOV declaration-only workflow described in historical sections: all new video output must have native gamut and transfer tags matching actual pixel encoding. Default SDR is Rec.709/sRGB. Pure Gamma 2.4/2.6 and AP1/ACEScct video remain visible but disabled with a reason, across all containers, CLI/MCP and legacy presets. Comments and unspecified tags cannot satisfy the requirement. EXR scene encodings and DPX numeric Gamma remain available. Missing/mismatched actual codec tags or MOV/MP4 colr prevents publication.

## Do's and Don'ts
Float animation deliveries in master mode retain one EXR sequence on the selected export volume. The existing native master-download link is labeled “下载 ACEScg 母版 ZIP”; a panel-description states that ZIP is generated on download without occupying the export disk and interrupted downloads restart. Keep the link native so a large archive is never buffered in the React application. Reuse the existing action row, Button/link states and generated tokens; regenerate and check tokens.css from the unchanged frontmatter.

DeliveryDestination owns one full-width parent-directory field above the format editor. ExportFolders owns the shared “选择文件夹” and “打开导出文件夹” actions for both renderers and sequence copies. Reuse Field, Button and Notice with the existing neutral surface, mono paths and generated spacing/border/focus tokens. The system picker replaces the permitted-roots disclosure; manual absolute paths remain editable. Selection/cancellation/error states retain the draft, keep button geometry stable and block submission until selection finishes. Completed exports show an open-folder button beside the copyable path; open failures remain local to that action. Each delivery creates a scene-named task folder containing frozen source, an sRGB thumbnail and outputs. No new palette or token values are introduced; regenerate and check tokens.css from the existing frontmatter.

Output color space and transfer function / Gamma use separate full-width native selects in DeliveryEditor. Gamut and transfer remain independent; P3-D65 plus sRGB is Display P3, and a gamut change preserves a compatible transfer. Read-only Color Space Tag and Gamma Tag fields describe the actual output without another conversion. Videos require exact native tags: Gamma 2.4/2.6 and AP1/ACEScct video remain visible but disabled with reasons, including MOV. Missing identifiers, comments or unspecified tags cannot certify these combinations. Default SDR is Rec.709/sRGB; Gamma 2.2, PQ and HLG remain available where actual container/codec tests pass. Current MXF writing drops sRGB transfer and disables that combination. EXR scene encodings and DPX numeric Gamma retain their image workflows. Target display luminance remains fixed by the actual ACES view (100 cd/m² SDR, 1000 cd/m² HDR, none for scene encodings). Changing transfer never selects a DCI white point or a different viewing luminance. Existing Field/native-select and generated tokens own all controls.

Show actual values, verified format combinations, and persistent errors. Distinguish original ACEScg data from sRGB preview. Preserve drafts on errors and detect revision conflicts. Do not fabricate timing estimates or output verification. Display HDR viewing limitations beside controls where relevant.

Gamut, gamma and data-level lists retain visible disabled options with explicit reasons. A menu with multiple entries stays expandable even when only one is available; a true single-entry control stays disabled. Use P3-D65 and BT.2020 / Rec.2020 names; distinguish AP1 log ACEScct from linear ACEScg. Data levels use Auto, Video (Limited) and Full as in Resolve. Auto is the default and displays its resolved range beside the control; jobs freeze that concrete range. ProRes types spell out 422 HQ, 422 LT and 422 Proxy, without changing the independently reported encoder precision. Scene-log and scene-linear encodings both show no display peak luminance.

MotionSourceEditor extends the animation code surface with a native source-file select and existing Button actions for adding/removing chapter modules. Reuse Field, Modal and the existing editor geometry; retain the established neutral palette, Chinese sans labels and mono code. File selection changes only the displayed source, while all chapters share the parent revision-safe draft and preview. No new visual tokens; regenerate/check tokens.css from the unchanged frontmatter.


MotionWorkbench adds an explicit “导出模式” native select: “保留 EXR 母版” (unchanged default) or “仅视频”. Video-only hides EXR precision, keeps a compatible selected video or selects an available video, and excludes the master-only video choice. Toggling back restores the last chosen EXR depth. Preview, source drafts and saved revisions are unaffected. Discovery advertises video_only; older services cannot offer the mode. MotionStorageEstimate and DeliveryDestination describe the selected outputs accurately: video-only writes no EXR, retains one transient float transport on the destination, encodes each frame once, and uses a content-dependent compressed video budget rather than a full raw sequence. Interruptions require an explicit restart from frame zero; already-published bundles recover without rendering. Reuse Field/native select, Notice, specification-summary and existing palette/type/spacing tokens; regenerate/check tokens.css with unchanged frontmatter.
