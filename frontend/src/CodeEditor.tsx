import CodeMirror from "@uiw/react-codemirror";
import { javascript } from "@codemirror/lang-javascript";
import { EditorView } from "@codemirror/view";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { tags } from "@lezer/highlight";

// The editor loads its language support locally and consumes the workstation's
// generated tokens. Tab leaves the editor so keyboard focus is never trapped.
const theme = EditorView.theme(
  {
    "&": {
      backgroundColor: "var(--color-background)",
      color: "var(--color-text)",
      fontSize: "12px",
    },
    ".cm-content": {
      fontFamily: "var(--font-mono)",
      caretColor: "var(--color-focus)",
    },
    ".cm-gutters": {
      backgroundColor: "var(--color-surface)",
      color: "var(--color-muted)",
      borderColor: "var(--color-border)",
    },
    ".cm-activeLine, .cm-activeLineGutter": {
      backgroundColor: "var(--color-raised)",
    },
    "&.cm-focused": {
      outline: "2px solid var(--color-focus)",
      outlineOffset: "-2px",
    },
    ".cm-scroller": { overflow: "auto", scrollbarGutter: "stable" },
  },
  { dark: true },
);
const extensions = [
  // The default light syntax colors lose contrast on the neutral dark canvas.
  syntaxHighlighting(
    HighlightStyle.define([
      { tag: tags.keyword, color: "var(--color-primary)" },
      { tag: tags.string, color: "var(--color-success)" },
      { tag: [tags.number, tags.bool], color: "var(--color-warning)" },
      { tag: tags.comment, color: "var(--color-muted)" },
      {
        tag: [tags.typeName, tags.function(tags.variableName)],
        color: "var(--color-focus)",
      },
    ]),
  ),
  javascript({ jsx: true, typescript: true }),
  EditorView.contentAttributes.of({
    "aria-label": "动画 TSX 代码",
    "aria-describedby": "motion-code-help",
  }),
];

export function CodeEditor({
  value,
  onChange,
  onComposing,
}: {
  value: string;
  onChange: (value: string) => void;
  onComposing: (value: boolean) => void;
}) {
  return (
    <div
      className="motion-code-editor"
      onCompositionStart={() => onComposing(true)}
      onCompositionEnd={() => onComposing(false)}
    >
      <CodeMirror
        value={value}
        onChange={onChange}
        theme={theme}
        extensions={extensions}
        height="540px"
        indentWithTab={false}
        onCreateEditor={(view) => {
          // CodeMirror keeps its scroll wrapper at tabindex=-1 by default.
          // Make the overflow surface independently keyboard-scrollable too.
          view.scrollDOM.tabIndex = 0;
          view.scrollDOM.setAttribute("aria-label", "代码滚动区域");
          view.scrollDOM.setAttribute("role", "region");
        }}
        basicSetup={{ autocompletion: true, foldGutter: true }}
      />
    </div>
  );
}
