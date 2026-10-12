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
    "primary": "#88BBC8"
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
      "backgroundColor": "#343434",
      "rounded": "{rounded.panel}"
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
Desktop: project/scene rail, central preview and shader editor, output inspector. Jobs remain visible below. At 1100px condense the rail; at 800px stack panels with document scrolling. Preview media reserves its aspect ratio. Long forms scroll naturally; tables own only their horizontal overflow.

## Elevation & Depth
Flat panels with fine borders. Modal dialogs alone use a restrained shadow and an inert backdrop.

## Shapes
6px controls and 8px preview frames. No ornamental pill cards.

## Components
Shared Button, Field, Textarea, Modal, Notice and Search own their states. DeliveryEditor owns each user-defined output target and reuses native selects through Field. The core delivery capability matrix owns dropdown options, compatibility and normalization; no preset is selected in the workbench. Native selects intentionally retain OS popup ownership. Buttons preserve dimensions while pending. Code editing remains available without a GPU preview. Scrollbar tokens apply globally. Visible keyboard focus and reduced-motion support are required.

## Do's and Don'ts
Show actual values, verified format combinations, and persistent errors. Distinguish original ACEScg data from sRGB preview. Preserve drafts on errors and detect revision conflicts. Do not fabricate timing estimates or output verification. Display HDR viewing limitations beside controls where relevant.
