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
  "layers": {
    "preview": 600
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
A four-stage strip is the workflow signature: choose a scene → edit and preview → configure delivery → jobs and artifacts. The selected project, scene and save state share one line above it. On wide windows each stage is a single line with its name on the left and live state on the right; from 1200px down the state stacks under the name. Stage numbers express real order; stages are freely revisitable views, not a locked wizard. The strip already names the stage, so the stage heading is a compact row: heading and the stage action. Repeated instructions are omitted; empty states retain a short next step.

The global topbar wraps its brand, navigation and engine status according to available width, including just above the 560px compact breakpoint. Navigation stays together and engine status remains right aligned; long device names wrap within the header without clipping text or shrinking the connection indicator. The compact layout retains its existing spacing and full-width navigation. Reuse existing tokens; regenerate/check tokens.css from the unchanged frontmatter.

Desktop: one WorkspaceLibrary owns the project and work pickers, new-work engine selector and optional examples above the common workflow. All engines use source on the left and preview/timeline on the right, with canvas/time and engine-specific parameters beneath the picture. The columns share a 1.15:1 ratio and stack below 900px. Native selectors retain OS popup ownership; the library wraps below 900px and stacks below 560px. Delivery and results retain their engine-specific controls inside the same named stages, using the same project context. On windows shorter than 760px, the delivery review/action bar stays in normal document flow so it cannot obscure keyboard-focused fields. No page-height lock or nested document scroller is added.

## Elevation & Depth
Flat panels with fine borders. Modal dialogs alone use a restrained shadow and an inert backdrop.

## Shapes
6px controls and 8px preview frames. No ornamental pill cards.

## Components
The existing native project pickers in both workspaces share `projects.ts` to read every API page before replacing the displayed list. Keep the established select styling and selection/draft guards; a later-page failure preserves the previous complete list. No visual tokens change.

PreviewPanel in components.tsx owns the shared expanded-preview modal variant for native and animation viewers. “大屏查看” expands the same mounted media and playback controls to the viewport; “退出大屏” and Escape restore its inline layout and trigger focus. Keep the neutral image surround, existing Button/Notice/spacing/type roles, and contain the picture without cropping. In this variant the section itself provides modal focus containment, inert sibling branches and document scroll locking so opaque iframes and audio clocks are never reparented or remounted. Its header stays reachable on short/narrow screens; only the expanded viewer scrolls while open. The layers.preview extension generates --layer-preview through scripts/design_tokens.py; native Modal dialogs retain the browser top layer. Live WGSL and float-animation rasters fit 1280×720 landscape or 720×1280 portrait (square: 720×720), without upscaling small sources. SDR DOM previews rerender to the available viewport. Original color/alpha, source dimensions, rational time and final delivery settings remain separate from this viewing budget. The expanded media plane alone grows into remaining height; transport rows use intrinsic height, never a percentage flex-basis. On desktop, PreviewTimeline places playback/stepping, the seek slider and display/recompile tools in one compact row. Narrow views use two rows while retaining native labels, keyboard access and touch targets. The status reserves one text line; optional color/precision guidance lives in a collapsed “预览说明” disclosure. Audio monitoring remains accessible without stretching the transport. At 1440×1000, 1512×760 and 1024×700, animation media should occupy more than 70% of viewer height; desktop timeline height stays at or below 48px. No media remount, source raster, color transform or playback-clock change is permitted.

MotionStorageEstimate owns the read-only disk budget inside animation export, below DeliveryDestination. Reuse section-heading, Button, Notice, specification-summary and the native technical-note disclosure. Show uncompressed EXR size separately from the destination budget and observed free space; separate volumes show workspace space independently. Existing palette/type/spacing tokens remain unchanged; regenerate/check tokens.css together. Keep loading/status geometry reserved, wrap narrow summary values, and never present compressed size as known.

ProjectAudio owns one shared “项目音频” dialog for native and animation workspaces. Reuse Button, Field, Modal, Notice and native selects/file/audio controls. Two-column timing/gain fields collapse to one below 560px; bounded asset pages show five native audio players, without autoplay. The existing dark neutral palette, spacing, mono/sans typography and focus roles remain unchanged. Regenerate/check tokens.css from the unchanged frontmatter. Timeline editing is explicit and revision checked, separate from source audition. PreviewAudio owns synchronized saved-track monitoring for both live WGSL and animation previews, with a native 0–100% volume slider, speaker state icon and fixed-width percentage below playback. Zero means mute; the slider reuses existing focus and primary tokens and fits narrow previews. Keep shared Button retry actions and Notice errors. Use existing tokens; no palette or layout redesign.

App owns the single “工作区”, shared project selection, save action and WorkflowNav. WorkspaceLibrary merges existing WGSL and motion records without rewriting stored revisions; motion results use bounded pages and pin the selected off-page record. “新建类型” offers WGSL, Three.js float HDR/SDR and React SDR; “新建作品” opens its source in the common editor stage. CodeEditor uses locally bundled CodeMirror for all sources; WGSL has no misleading JavaScript grammar. PreviewTimeline shares explicit play/pause, first/previous/next-frame actions and a native integer-frame slider; each adapter retains its exact rational frame rate. Hidden adapters preserve drafts and stop playback. Project changes guard both visible and hidden drafts; replacing a work within an adapter retains its existing discard/conflict protection. The save button occupies the common stage heading. Existing palette, spacing and typography tokens remain unchanged; regenerate/check tokens.css.

MotionPreview owns sandboxed Three.js RGBA32F candidates and the last working picture. HDR three-float-v1 factories coexist with SDR remotion-sdr-v1 React/DOM/ThreeCanvas compositions. The shared “新建作品” action uses the selected engine; all examples remain under the native “示例模板（可选）” disclosure. Creation reuses Button/Field tokens without new visual values. Historical SDR records open without rewriting revisions. Compile/runtime/float-capability errors retain the last picture; hidden views pause playback. A generated shader from the pinned OCIO ACES SDR transform produces sRGB display copies without altering the float target. MotionWorkbench's export area reuses Field/native selects for EXR Half/Float and HDR PQ/HLG or SDR sRGB/Rec.709 video; discovery owns options and unavailable encoders. Delivery selection never changes preview colors. Engine-specific discovery hides EXR fields for SDR source and resets incompatible outputs on source changes; all sources retain playback and draft/recovery controls. Result actions expose the master ZIP, HDR video and verification report, with a separately labeled sRGB thumbnail. DeliveryDestination's animation variant describes source/EXR/HDR bundles. All visual roles use existing generated tokens; no new palette or spacing values. Drafts, revision conflicts and jobs survive navigation.

Animation discovery reserves the existing status line for loading feedback and uses Notice for actionable service/retry errors. Keep successfully loaded projects visible during partial discovery failure; show the project-creation empty state only after a confirmed empty project response. These recovery states reuse the existing controls and generated tokens, without introducing new visual values.

External animation saves update the current editor automatically through revision reads; no additional refresh control is introduced. The shared status line acknowledges an accepted external revision without moving focus. Notice beside the code actions owns persistent draft-conflict and synchronization-error feedback; copy/reload and the existing connection control retain their established Button/Modal behavior. Updates preserve the editor instance and preview surface geometry. Reuse the existing semantic, spacing and typography tokens; regenerate and check tokens.css from the unchanged frontmatter.

ColorWorkflow owns the shared read-only color explanation beside preview settings and delivery specifications. Code generation and internal processing are fixed linear ACEScg/AP1; the browser always uses the ACES 2.0 sRGB view, independently of user-selected delivery gamut/gamma. Reuse the section-heading, frame-settings, specification-summary and technical-note patterns with existing generated spacing/type tokens. Parameter storage labels explicitly describe parameter RGB only; the shader receives ACEScg. No input/work-space dropdown is introduced. This copy-only extension adds no visual tokens; regenerate and check tokens.css against the unchanged frontmatter.

The preview opens in live draft mode. Its inspector owns the full canvas specification form, below the scene parameters: size preset, width, height, rational frame rate, frame count, final-render sampling and start timecode. Delivery reuses those controlled values in a read-only summary with a return-to-preview action. Both sections reuse Field, Button and the generated spacing, border and typography tokens; only one canvas form exists. Its segmented control shares Button states with stored keyframes and motion review. Play/pause, a native keyboard-operable timeline and the current-frame input sit below the existing fixed media surface. Low-resolution dimensions, single sampling and stale/error feedback remain visible in a reserved status row. Live images share the same contained geometry and alpha checkerboard as stored previews; `colors.previewBackground` and `colors.previewChecker` generate the shared media tokens. Original pixel readouts remain unavailable for transient proxies; the readout lays R, G, B and A out as one row with each value beside its letter. No animation starts without Play, and hidden views stop preview work. Stored motion preview accepts only verified sRGB video; the unavailable control explains that legacy or other-Gamma video needs a newly generated sRGB preview. Reuse the existing Button and title pattern without new tokens.

ColorField owns scene color selection through the shared Modal, Field, Button and Notice. Its primary control is an ACES-rendered color swatch opening an OKLCH lightness/chroma plane and native hue/lightness/chroma sliders. Hatched areas mark colors beyond sRGB; text explains chroma reduction at fixed lightness and hue. Original/new swatches are separate from stored RGB, and the RGB editor is a native advanced disclosure. The palette is the only expressive color surface. `colorPickerMarker` adapts its light foreground and dark background roles to a ring and outer outline, leaving the selected color visible through the center. `colors.pickerLight` / `colors.pickerDark` generate `--color-pickerLight` / `--color-pickerDark` for these roles; all other picker chrome reuses existing semantic and spacing tokens.

Shared Button, Field, Textarea, Modal, Notice and Search own their states. WorkflowNav owns the named stage strip; active stage uses an underline and explicit aria-current. Each stage has one heading and a clear next action. Delivery review shows actual values; only a successful submission navigates to jobs. Code remains visible beside the preview in the shared editing stage. DeliveryEditor owns each user-defined output target and reuses native selects through Field. The core delivery capability matrix owns dropdown options, compatibility and normalization; no preset is selected in the workbench. Native selects intentionally retain OS popup ownership. Buttons preserve dimensions while pending. Code editing remains available without a GPU preview. Scrollbar tokens apply globally. Visible keyboard focus and reduced-motion support are required.


Master-first animation delivery: new blank animations use the float engine; SDR remains explicitly selectable. Final output defaults to ACEScg Float32 ZIP masters only and 2×2 spatial supersampling, with 1×1 and 4×4 alternatives; the low-resolution preview stays single-sampled. MotionWorkbench owns editable positive-integer custom dimensions/frame count and a rational numerator/denominator text field alongside rate presets. GPU, memory and codec limitations fail explicitly. Use existing Field, Button, Modal and Notice tokens; no new visual tokens. Regenerate/check tokens.css from the unchanged frontmatter.

MotionWorkbench distinguishes measured float-pipeline verification from revision-bound source-color review. An optional shared Modal records reviewer and evidence against the revision captured when opened; conflicts retain form text, and cancellation changes no review. A saved review is an explicit attestation, not automatic shader certification. Unreviewed shaders remain usable. Dirty/new revisions show unreviewed; job results show the frozen review and independent native video tag verification.

Both animation engines stage intermediate frames, bundler outputs, encoder files and final output on the selected export volume. Workspace keeps task metadata/logs; system browser temporary profiles may remain local. Missing/replaced volume fails rather than falling back locally. DeliveryDestination explains direct external-disk writing. Master-mode float recovery retains verified EXRs; video-only and SDR rerender on retry.

Mandatory exact video-tag policy supersedes the earlier MOV declaration-only workflow described in historical sections: all standard display video output must have native gamut and transfer tags matching actual pixel encoding. Default SDR is Rec.709/sRGB. Pure Gamma 2.4/2.6 and legacy AP1/ACEScct video remain visible but disabled with a reason, across all containers, CLI/MCP and legacy presets. Comments and unspecified tags cannot satisfy the requirement. EXR scene encodings and DPX numeric Gamma remain available. Missing/mismatched actual codec tags or MOV/MP4 colr prevents publication.

## Do's and Don'ts
Float animation deliveries in master mode retain one EXR sequence on the selected export volume. The existing native master-download link is labeled “下载 ACEScg 母版 ZIP”; a panel-description states that ZIP is generated on download without occupying the export disk and interrupted downloads restart. Keep the link native so a large archive is never buffered in the React application. Reuse the existing action row, Button/link states and generated tokens; regenerate and check tokens.css from the unchanged frontmatter.

DeliveryDestination owns one full-width parent-directory field above the format editor. ExportFolders owns the shared “选择文件夹” and “打开导出文件夹” actions for both renderers and sequence copies. Reuse Field, Button and Notice with the existing neutral surface, mono paths and generated spacing/border/focus tokens. The system picker replaces the permitted-roots disclosure; manual absolute paths remain editable. Selection/cancellation/error states retain the draft, keep button geometry stable and block submission until selection finishes. Completed exports show an open-folder button beside the copyable path; open failures remain local to that action. Each delivery creates a scene-named task folder containing frozen source, an sRGB thumbnail and outputs. No new palette or token values are introduced; regenerate and check tokens.css from the existing frontmatter.

Output color space and transfer function / Gamma use separate full-width native selects in DeliveryEditor. Gamut and transfer remain independent; P3-D65 plus sRGB is Display P3, and a gamut change preserves a compatible transfer. Read-only Color Space Tag and Gamma Tag fields describe the actual output without another conversion. Videos require exact native tags: Gamma 2.4/2.6 and legacy AP1/ACEScct video remain visible but disabled with reasons, including MOV. Missing identifiers, comments or unspecified tags cannot certify these combinations. Default SDR is Rec.709/sRGB; Gamma 2.2, PQ and HLG remain available where actual container/codec tests pass. Current MXF writing drops sRGB transfer and disables that combination. EXR scene encodings and DPX numeric Gamma retain their image workflows. Target display luminance remains fixed by the actual ACES view (100 cd/m² SDR, 1000 cd/m² HDR, none for scene encodings). Changing transfer never selects a DCI white point or a different viewing luminance. Existing Field/native-select and generated tokens own all controls.

Show actual values, verified format combinations, and persistent errors. Distinguish original ACEScg data from sRGB preview. Preserve drafts on errors and detect revision conflicts. Do not fabricate timing estimates or output verification. Display HDR viewing limitations beside controls where relevant.

Gamut, gamma and data-level lists retain visible disabled options with explicit reasons. A menu with multiple entries stays expandable even when only one is available; a true single-entry control stays disabled. Use P3-D65 and BT.2020 / Rec.2020 names; distinguish AP1 log ACEScct from linear ACEScg. Data levels use Auto, Video (Limited) and Full as in Resolve. Auto is the default and displays its resolved range beside the control; jobs freeze that concrete range. ProRes types spell out 422 HQ, 422 LT and 422 Proxy, without changing the independently reported encoder precision. Scene-log and scene-linear encodings both show no display peak luminance.

MotionSourceEditor extends the animation code surface with a native source-file select and existing Button actions for adding/removing chapter modules. Reuse Field, Modal and the existing editor geometry; retain the established neutral palette, Chinese sans labels and mono code. File selection changes only the displayed source, while all chapters share the parent revision-safe draft and preview. No new visual tokens; regenerate/check tokens.css from the unchanged frontmatter.


MotionWorkbench adds an explicit “导出模式” native select: “保留 EXR 母版” (unchanged default) or “仅视频”. Video-only hides EXR precision, keeps a compatible selected video or selects an available video, and excludes the master-only video choice. Toggling back restores the last chosen EXR depth. Preview, source drafts and saved revisions are unaffected. Discovery advertises video_only; older services cannot offer the mode. MotionStorageEstimate and DeliveryDestination describe the selected outputs accurately: video-only writes no EXR, retains one transient float transport on the destination, encodes each frame once, and uses a content-dependent compressed video estimate rather than a full raw sequence. Interruptions require an explicit restart from frame zero; already-published bundles recover without rendering. Reuse Field/native select, Notice, specification-summary and existing palette/type/spacing tokens; regenerate/check tokens.css with unchanged frontmatter.

Animation output discovery includes Apple ProRes 422 HQ · MOV with explicit sRGB/PQ/HLG labels. SDR sources expose only its sRGB variant. Missing prores_ks keeps the option visible with an unavailable reason; a selected ProRes transfer survives master/video-only toggles. Existing native select layout, tokens, source precision labels and even-dimension policy remain applicable. ProRes video storage uses a compressed HQ macroblock target without percentage margins; the budget must not treat the compressed movie as an uncompressed frame sequence. Encoding quality is independent of this planning estimate.

Native linear animation colors: MotionWorkbench reuses Field/native selects, Button, Notice and frame-settings for the current source-space description, target-space selector and explicit “升级色彩管线” action. Missing source metadata means legacy Rec.709; new blank float content defaults to linear BT.2020; AP1 and AP0 are native alternatives. Upgrade requires a saved draft, creates a revision and preserves arbitrary old source in editable background layers; the “编辑图层” select shares MotionSourceEditor without losing drafts. Explain independent layer depth and no automatic saturation expansion. Display/PQ encoding remains an exit operation. Keep all palette/type/spacing tokens unchanged and regenerate/check tokens.css.


Low-resolution HDR animation preview: MotionPreview owns the existing Field/native “预览显示” selector (自动 HDR / sRGB SDR), status row, timeline and PreviewPanel. Auto targets wide-gamut float scenes only when HDR video display and actual HEVC decode/MSE support are reported. The opaque source iframe remains network/media-disabled and answers requested float captures; the parent mounts native video over the unchanged canvas. Local encode_motion_hdr_preview is shared by HTTP/MCP/CLI, accepts bounded chunks (up to 64 frames and 192 MiB), including GPU-packed P010 PQ and verifies PQ/HEVC 10-bit pixels/tags. Preview uses even rasters up to 1280x720 (orientation-aware), every frame at the original rational project rate; no revision, job or artifact is created. Alpha composites black in scene-linear light before one ACES PQ output transform; masters remain unchanged. Buffer ahead is bounded, consumed ranges evicted, out-of-buffer seeks replace generations, superseded replies are ignored, pause/hidden views stop generating new chunks, and replacement/unmount releases media and requests. Playback remains explicit through Play; buffering suspends soundtrack monitoring. Decode/encode failures preserve canvas and source, explain sRGB fallback and offer the existing recompile retry. Device capability is not physical HDR certification. Reuse existing palette/type/spacing tokens, regenerate/check tokens.css with unchanged frontmatter; no new visual token values.

Safari HDR compatibility: treat video-dynamic-range as unsupported when both high and standard queries are false; only then fall back to dynamic-range: high. A recognized SDR video plane remains authoritative. HEVC/MSE and the encoded PQ profile are independently checked. Listen to both display-query families, show distinct display/codec/raster fallback reasons, and label buffering separately from sRGB. Sandbox frame replies already yield asynchronously, so do not add a vsync wait for every captured sample.

720p HDR playback retains every source frame and the exact project rate. GPU OCIO renders the linear black composite once into PQ and packs 10-bit limited-range P010 with top-left chroma. Transferable buffers and an authenticated binary adapter avoid float/Base64 copies. Hardware encoding is accepted only after actual Main10 output verification; software fallback preserves dimensions and rate. SPS/container tags, decoded frame count and rational timing are checked. One strict decode replaces redundant decoding. One bounded capture batch overlaps encoding, with two seconds of startup/rebuffer protection and consumed-range eviction. Paused seeks render one sharp frame. Source edits, out-of-buffer seeks and unmount invalidate pending work; buffering preserves Pause/SDR controls. Reuse existing Field/status/PreviewPanel and unchanged generated visual tokens.

HDR streaming reuses one idle-expiring native encoder. Each returned chunk starts with a forced keyframe and passes independent strict decoding; internal drain pictures never reach the player. Actual chunk start timestamps are offset onto the project clock. The process expires after 15 idle seconds and closes with the daemon. GPU capture is a bounded cancellable batch, yielding between short groups, and uses transferable buffers.


Animation ACEScct intermediate and measured storage: MotionWorkbench adds only prores-4444-acescct for float sources, opaque native Apple VideoToolbox/P416/verified 12-bit MOV. The existing export mode and EXR defaults remain; preview is independent. This narrow declared-scene contract permits colr 2/2/1 only with an exact AP1/D60 ACEScct declaration, no display view and limited-range packing. It is neither a standardized ACES archive nor automatic Resolve color recognition. Other output tag gates remain strict. Unsupported backends remain visible with reasons, without XQ or 10-bit fallback.

MotionStorageEstimate preserves Field/Button/Notice and specification-summary styling. It shows estimated final size, phase-based peak disk occupancy, actual free space, and known unmeasured components. No percentage reserve or largest-sample extrapolation is allowed. Quick read-only estimates keep the 300 ms debounce. The explicit 采样估算/取消采样 flow uses the current full draft without saving it, runs at most one cancellable task per workspace, and reports progress. Measured means extrapolate EXR/ProRes sizes; source/files/layers/color/time/output changes immediately invalidate samples and cancel superseded work, whereas name-only changes do not. Old replies never replace current content. Matching completed samples can be submitted as storage_estimate_id; the server revalidates identity and current free space. Service interruption, cancellation and failure clean disposable samples on their original volume, retain diagnostics, and preserve drafts. Native controls, responsive wrapping, keyboard focus and live status remain canonical. DESIGN token frontmatter is unchanged; regenerate/check tokens.css.


采样生命周期：名称修改及保存触发的能力刷新保留正在运行的采样；源码、输出、目录或工作上下文改变后取消。取消和超时会对任务拥有的子进程先终止、再强制结束，初始化失败或线程中断会释放采样槽位。12-bit 能力探测或诊断文件写入失败仅禁用 4444，不影响已有输出格式。动画 EXR 与缩略图均不添加隐藏磁盘余量。


Animation delivery layout: MotionWorkbench owns a two-column motion-export-layout above 900px. Format/sampling choices occupy the left column; the existing DeliveryDestination and MotionStorageEstimate form the right column. Both columns align their headings and reuse Field/Button/Notice; remove redundant horizontal dividers in this scoped layout. The export action row spans both columns below them. At 900px and below the same DOM stacks settings, destination, budget and actions, preserving drafts, native select behavior, long-path editing and picker recovery. Keep natural document scrolling, minmax(0, 1fr) columns and min-width: 0; no fixed-height shell, duplicate controls or new visual tokens. Regenerate/check tokens.css with unchanged values.


Preview performance and concise copy: retain the existing neutral palette, font roles and spacing. ColorWorkflow uses a collapsed native details/summary in editing and delivery; pipeline upgrade consequences use the same disclosure. Default copy shows control labels, color/precision identifiers, save state and actionable errors without repeated stage instructions. CodeEditor and retained motion adapters skip unrelated playback updates; source serialization and message listeners stay stable across frames. Returning to an unchanged animation preserves its mounted preview and keeps playback paused. WGSL transient previews schedule at most 24 requests per second, serially, counting render time in that budget. HDR seeks reuse complete buffered frames where available (including looped copies); only missing frames rebuild the media generation. This affects transport and UI work only, not source resolution, rational frame time, sampling or ACES/alpha semantics. Regenerate/check tokens.css from the unchanged token frontmatter.
