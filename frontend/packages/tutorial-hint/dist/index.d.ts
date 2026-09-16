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
export declare function TutorialHint({ title, description, importance, children, className, style, }: TutorialHintProps): import("react").JSX.Element;
export default TutorialHint;
