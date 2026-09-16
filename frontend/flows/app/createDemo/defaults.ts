import type { GuidedStep } from "@/flows/app/createDemo/types";

/** Product context — Story fills these from smart suggestions + the AI brief. */
export const EMPTY_PRODUCT_CONTEXT = {
  whatItDoes: "",
  audience: "",
  whatMatters: "",
  whatToAvoid: "",
  toneNotes: "",
};

/** Empty guided list — use “Draft with AI” or paste your own steps. */
export const EMPTY_STEPS: GuidedStep[] = [];
