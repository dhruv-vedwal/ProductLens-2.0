import { jsx as _jsx } from "react/jsx-runtime";
/**
 * Optional marker for product UIs. ProductLens analyzer reads
 * `data-tutorial-hint` and elevates the node into knownSelectors.
 *
 * Never required — demos work without hints.
 */
export function TutorialHint({ title, description = "", importance = "high", children, className, style, }) {
    const payload = JSON.stringify({ title, description, importance });
    return (_jsx("div", { "data-tutorial-hint": payload, className: className, style: style, "data-pl-importance": importance, children: children }));
}
export default TutorialHint;
