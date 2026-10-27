import type { HTMLAttributes, PropsWithChildren } from "react";

export function Card({ className = "", ...props }: HTMLAttributes<HTMLElement>) {
  return <section className={`card ${className}`} {...props} />;
}

export function CardTitle({ children }: PropsWithChildren) {
  return <h2 className="card-title">{children}</h2>;
}
