import type { CSSProperties, ReactNode } from "react";

export type TutorialHintProps = {
  title: string;
  description?: string;
  importance?: "low" | "medium" | "high";
  children?: ReactNode;
  className?: string;
  style?: CSSProperties;
};

/**
 * Optional marker for product UIs. ProductLens analyzer reads
 * `data-tutorial-hint` and elevates the node into knownSelectors.
 *
 * Never required — demos work without hints.
 */
export function TutorialHint({
  title,
  description = "",
  importance = "high",
  children,
  className,
  style,
}: TutorialHintProps) {
  const payload = JSON.stringify({ title, description, importance });
  return (
    <div
      data-tutorial-hint={payload}
      className={className}
      style={style}
      data-pl-importance={importance}
    >
      {children}
    </div>
  );
}

export default TutorialHint;
