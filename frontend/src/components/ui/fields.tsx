import { cloneElement, type ReactElement, type ReactNode } from "react";

type FieldControlProps = {
  id?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean;
};

export type FieldProps = {
  id: string;
  label: string;
  error?: string;
  hint?: ReactNode;
  children: ReactElement<FieldControlProps>;
};

export function FormField({ id, label, error, hint, children }: FieldProps) {
  const descriptionId = error || hint ? `${id}-description` : undefined;

  return (
    <div className="grid gap-1 text-sm">
      <label htmlFor={id} className="font-medium text-ink-muted">
        {label}
      </label>
      {cloneElement(children, {
        id,
        "aria-invalid": Boolean(error),
        "aria-describedby": descriptionId,
      })}
      {descriptionId ? (
        <span
          id={descriptionId}
          className={error ? "text-danger" : "text-ink-dim"}
        >
          {error ?? hint}
        </span>
      ) : null}
    </div>
  );
}

export function SelectField(props: FieldProps) {
  return <FormField {...props} />;
}
