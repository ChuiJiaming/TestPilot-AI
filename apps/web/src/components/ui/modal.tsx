import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

export function Modal({ title, description, onClose, children, wide }: {
  title: string; description?: string; onClose: () => void; children: ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = ""; };
  }, [onClose]);

  return createPortal(
    <div className="modal-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className={wide ? "modal-card modal-wide" : "modal-card"} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <div><h3>{title}</h3>{description && <p>{description}</p>}</div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="关闭"><X size={15} /></button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  );
}
