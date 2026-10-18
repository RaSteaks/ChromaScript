import {
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
  type Ref,
  type ButtonHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";
import { X, Maximize2, Minimize2, Search as SearchIcon } from "lucide-react";

/** Canonical controls retain native keyboard behavior and stable pending geometry. */
export function Button({
  children,
  className = "",
  onClick,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { ref?: Ref<HTMLButtonElement> }) {
  return (
    <button
      type="button"
      className={"button " + className}
      onClick={(event) => {
        // WebKit does not focus pointer-activated buttons by default. Keep a
        // concrete opener for shared dialog/viewer focus restoration.
        event.currentTarget.focus({ preventScroll: true });
        onClick?.(event);
      }}
      {...props}
    >
      {children}
    </button>
  );
}
/** Shared editors keep the layout fixed while retaining native selection and scrolling. */
export function Textarea({
  className = "",
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={"resize-none " + className} {...props} />;
}
export function Field({
  label,
  id,
  hint,
  invalid = false,
  children,
}: {
  label: string;
  id: string;
  hint?: string;
  invalid?: boolean;
  children: ReactNode;
}) {
  return (
    <div className={"field " + (invalid ? "invalid" : "")}>
      <label htmlFor={id}>{label}</label>
      {children}
      {hint && <small id={id + "-help"}>{hint}</small>}
    </div>
  );
}
export function Notice({
  text,
  error = false,
  onClose,
}: {
  text: string;
  error?: boolean;
  onClose?: () => void;
}) {
  if (!text) return null;
  return (
    <div
      className={"notice " + (error ? "error" : "")}
      role={error ? "alert" : "status"}
    >
      <span>{text}</span>
      {onClose && (
        <Button aria-label="关闭提示" onClick={onClose}>
          <X size={14} />
        </Button>
      )}
    </div>
  );
}
export function Modal({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const node = ref.current;
    if (!open) {
      if (node?.open) node.close();
      return;
    }
    const trigger = document.activeElement;
    if (!node?.open) node?.showModal();
    return () => {
      // A conditional editor may unmount its dialog on cancel. Close it first
      // so focus returns to the trigger even when the native node is removed.
      if (node?.open) node.close();
      if (trigger instanceof HTMLElement && trigger.isConnected)
        trigger.focus();
    };
  }, [open]);
  return (
    <dialog
      ref={ref}
      aria-label={title}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className="modal-heading">
        <h2>{title}</h2>
        <Button aria-label="关闭对话框" onClick={onClose}>
          <X size={18} />
        </Button>
      </div>
      {children}
    </dialog>
  );
}

export type PreviewPanelHandle = { close: () => void };

/** Expanded-preview modal variant: keep media in its original DOM location.
 * Reparenting an iframe or mounting a second player would reset its clock/audio. */
export function PreviewPanel({
  label,
  toolbar,
  children,
  className = "",
  active = true,
  ref,
}: {
  label: string;
  toolbar?: ReactNode;
  children: ReactNode;
  className?: string;
  active?: boolean;
  ref?: Ref<PreviewPanelHandle>;
}) {
  const panel = useRef<HTMLElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const restoreFocus = useRef<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const open = expanded && active;
  useImperativeHandle(ref, () => ({ close: () => setExpanded(false) }), []);
  useEffect(() => {
    if (!active) setExpanded(false);
  }, [active]);

  useLayoutEffect(() => {
    const node = panel.current;
    if (!open || !node) return;
    if (restoreFocus.current !== null)
      cancelAnimationFrame(restoreFocus.current);
    const trigger = toggle.current;
    const inert = new Map<HTMLElement, boolean>();
    // Isolate sibling branches all the way to body without making this media's
    // ancestors inert. Restore existing states rather than unlocking other UI.
    let branch: HTMLElement = node;
    while (branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        if (sibling instanceof HTMLElement && sibling !== branch) {
          inert.set(sibling, sibling.inert);
          sibling.inert = true;
        }
      }
      branch = branch.parentElement;
      if (branch === document.body) break;
    }
    const rootOverflow = document.documentElement.style.overflow;
    const bodyOverflow = document.body.style.overflow;
    document.documentElement.style.overflow = "hidden";
    document.body.style.overflow = "hidden";
    trigger?.focus({ preventScroll: true });
    const keyboard = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      if (event.key === "Escape") {
        event.preventDefault();
        setExpanded(false);
      }
      if (event.key !== "Tab") return;
      const controls = Array.from(
        node.querySelectorAll<HTMLElement>(
          'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), video[controls], [tabindex="0"]',
        ),
      ).filter(
        (element) =>
          element.tabIndex >= 0 && element.getClientRects().length > 0,
      );
      // Safari's system keyboard preference may skip buttons entirely. Own
      // traversal for this modal variant so Tab never escapes into browser chrome.
      if (!controls.length) return;
      event.preventDefault();
      const current = controls.indexOf(document.activeElement as HTMLElement);
      const next =
        current < 0
          ? 0
          : (current + (event.shiftKey ? -1 : 1) + controls.length) %
            controls.length;
      controls[next].focus();
    };
    document.addEventListener("keydown", keyboard);
    return () => {
      document.removeEventListener("keydown", keyboard);
      for (const [element, previous] of inert) element.inert = previous;
      document.documentElement.style.overflow = rootOverflow;
      document.body.style.overflow = bodyOverflow;
      if (trigger?.isConnected) trigger.focus({ preventScroll: true });
      // An iframe's Escape event can retain focus through the current browser
      // event; restore again after the inline layout has committed.
      restoreFocus.current = requestAnimationFrame(() => {
        if (trigger?.isConnected) trigger.focus({ preventScroll: true });
      });
    };
  }, [open]);

  return (
    <section
      ref={panel}
      className={`preview-panel ${className}${open ? " is-expanded" : ""}`}
      role={open ? "dialog" : "region"}
      aria-modal={open || undefined}
      aria-label={open ? `${label} · 大屏查看` : label}
    >
      <div className="preview-panel-toolbar">
        {toolbar}
        <Button
          ref={toggle}
          className="preview-expand"
          aria-expanded={open}
          title={open ? "退出大屏（Esc）" : "放大预览画面"}
          onClick={() => setExpanded((value) => !value)}
        >
          {open ? (
            <Minimize2 size={16} aria-hidden="true" />
          ) : (
            <Maximize2 size={16} aria-hidden="true" />
          )}
          {open ? "退出大屏" : "大屏查看"}
        </Button>
      </div>
      <div className="preview-panel-content">{children}</div>
    </section>
  );
}

export function Search({
  value,
  onChange,
  id,
  label,
}: {
  value: string;
  onChange: (value: string) => void;
  id: string;
  label: string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  return (
    <div className="search">
      <SearchIcon size={15} />
      <label className="sr-only" htmlFor={id}>
        {label}
      </label>
      <input
        ref={ref}
        id={id}
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={label}
      />
      {value && (
        <Button
          aria-label="清除搜索"
          onClick={() => {
            onChange("");
            ref.current?.focus();
          }}
        >
          <X size={14} />
        </Button>
      )}
    </div>
  );
}
