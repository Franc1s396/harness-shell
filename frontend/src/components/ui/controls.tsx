import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";
export type TooltipPlacement = "bottom" | "bottom-start" | "right";

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
};

const variants: Record<ButtonVariant, string> = {
  primary: "bg-accent text-app hover:brightness-110",
  secondary:
    "border border-line bg-raised text-ink hover:border-line-strong",
  danger: "bg-danger text-app hover:brightness-110",
  ghost: "bg-transparent text-ink-muted hover:bg-raised hover:text-ink",
};

export function Button({
  variant = "primary",
  className = "",
  type = "button",
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      className={`rounded-md px-3 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${variants[variant]} ${className}`}
      {...props}
    />
  );
}

export function IconButton({
  label,
  children,
  className = "",
  type = "button",
  tooltipPlacement = "bottom",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  label: string;
  children: ReactNode;
  tooltipPlacement?: TooltipPlacement;
}) {
  return (
    <Tooltip text={label} placement={tooltipPlacement}>
      <button
        type={type}
        aria-label={label}
        className={`grid size-8 place-items-center rounded-md text-ink-muted transition hover:bg-raised hover:text-ink disabled:cursor-not-allowed disabled:opacity-40 ${className}`}
        {...props}
      >
        {children}
      </button>
    </Tooltip>
  );
}

const tooltipPlacementClasses: Record<TooltipPlacement, string> = {
  bottom: "top-full left-1/2 mt-2 -translate-x-1/2",
  "bottom-start": "top-full left-0 mt-2",
  right: "left-full top-1/2 ml-2 -translate-y-1/2",
};

export function Tooltip({
  text,
  children,
  placement = "bottom",
}: {
  text: string;
  children: ReactNode;
  placement?: TooltipPlacement;
}) {
  return (
    <span className="group relative inline-flex">
      {children}
      <span
        role="tooltip"
        className={`pointer-events-none absolute z-50 hidden whitespace-nowrap rounded bg-raised px-2 py-1 text-xs text-ink shadow-lg group-hover:block group-focus-within:block ${tooltipPlacementClasses[placement]}`}
      >
        {text}
      </span>
    </span>
  );
}
