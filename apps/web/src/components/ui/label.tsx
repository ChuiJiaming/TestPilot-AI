import type { LabelHTMLAttributes, PropsWithChildren } from "react";

export function Label({ children, className = "", ...props }: PropsWithChildren<LabelHTMLAttributes<HTMLLabelElement>>) {
  return <label className={`label ${className}`} {...props}>{children}</label>;
}
