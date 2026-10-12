import {
  useEffect,
  useRef,
  type ButtonHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";
import { X, Search as SearchIcon } from "lucide-react";

/** Canonical controls retain native keyboard behavior and stable pending geometry. */
export function Button({
  children,
  className = "",
  onClick,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      className={"button " + className}
      onClick={onClick}
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
