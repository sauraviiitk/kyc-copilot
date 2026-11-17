import type { ReactNode } from "react";

import { cn } from "../../lib/utils";
import { Button } from "./button";

interface FieldProps {
  label: string;
  htmlFor?: string;
  error?: string | null;
  hint?: string | null;
  required?: boolean;
  className?: string;
  children: ReactNode;
  action?: ReactNode;
}

export function Field({ label, htmlFor, error, hint, required, className, children, action }: FieldProps) {
  return (
    <div className={cn("space-y-1.5", className)}>
      <div className="flex items-baseline justify-between gap-2">
        <label htmlFor={htmlFor} className="label-text mb-0">
          {label}
          {required ? <span className="ml-0.5 text-rose-500">*</span> : null}
        </label>
        {action}
      </div>
      {children}
      {error ? <p className="text-xs font-medium text-rose-600">{error}</p> : null}
      {!error && hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

export function InlineAlert({
  tone = "info",
  title,
  children,
  onDismiss,
}: {
  tone?: "info" | "warning" | "error" | "success";
  title?: string;
  children?: ReactNode;
  onDismiss?: () => void;
}) {
  const tones = {
    info: "border-sky-200 bg-sky-50 text-sky-900",
    warning: "border-amber-200 bg-amber-50 text-amber-900",
    error: "border-rose-200 bg-rose-50 text-rose-900",
    success: "border-emerald-200 bg-emerald-50 text-emerald-900",
  } as const;
  return (
    <div className={cn("flex items-start justify-between gap-3 rounded-lg border px-3 py-2 text-xs", tones[tone])}>
      <div>
        {title ? <p className="font-semibold">{title}</p> : null}
        {children ? <div className={title ? "mt-0.5" : ""}>{children}</div> : null}
      </div>
      {onDismiss ? (
        <Button variant="ghost" size="sm" className="h-6 px-2 text-[11px]" onClick={onDismiss}>
          Dismiss
        </Button>
      ) : null}
    </div>
  );
}
