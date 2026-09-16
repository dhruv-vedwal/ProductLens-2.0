# Optional TutorialHint markers (`@productlens/tutorial-hint`)

> Small React helper so product authors can mark “this control matters.” ProductLens **never requires** it — demos must work on arbitrary URLs without source access.

---

## 1. Problem this solves

**Issue:** Pure exploration invents importance from heuristics (`data-tour`, visible text, LLM guesses). That is good, but the **app author** often knows better: “this is the primary CTA,” “this panel is the money moment.” Without a standard hook, every customer invents their own attributes and we cannot detect them reliably.

**Fix:** Optional, documented `TutorialHint` contract:

- Author wraps UI → emits `data-tutorial-hint` JSON.  
- Exploration elevates those nodes into the Verified Application Model with **high** importance.  
- If the attribute is missing, nothing breaks — Mode 1 still works.

| Approach | Failure | TutorialHint |
|---|---|---|
| Mandatory SDK | Blocks Mode 1 on apps you don’t control | Optional forever (harness lock) |
| Only LLM guesses importance | Misses obvious CTAs | Author signal when available |
| Undocumented `data-*` soup | Explorer cannot trust shapes | One package + one attribute |

This pairs with Application Intelligence: hints bias **what to explore/film**, they do not replace verified workflows.

---

## 2. Install

```bash
npm install @productlens/tutorial-hint
# or link locally from frontend/packages/tutorial-hint
```

---

## 3. Usage

```tsx
import { TutorialHint } from "@productlens/tutorial-hint";

export function PricingCta() {
  return (
    <TutorialHint
      title="Upgrade"
      description="Primary paid conversion"
      importance="high"
    >
      <button>Start trial</button>
    </TutorialHint>
  );
}
```

Props: `title`, `description?`, `importance?: "low" | "medium" | "high"`.

The wrapper renders a `div` with `data-tutorial-hint={JSON.stringify({ title, description, importance })}`.

---

## 4. How ProductLens uses it

1. Exploration (passive observe) inventories `[data-tutorial-hint]`.  
2. Hint payloads merge into the Verified Application Model with elevated confidence.  
3. Entries land in `graph_json` (compatibility: `tutorialHints` / high-importance selectors).  
4. Story planner / camera cues prefer those surfaces when they sit on certified/partial workflows.

Deep model shape: [`learn/19-application-map-analyzer.md`](../../learn/19-application-map-analyzer.md).

---

## 5. Interview line

**Q: Why ship an npm package if the feature is optional?**  
**A:** Optional for *customers who don’t control the target app*; valuable for *customers who do* (e.g. dogfooding ProductLens itself). The contract must be stable and discoverable — a package + README beats a wiki note. After the intelligence rewrite, hints are evidence boosters, not a substitute for verified behavior.
