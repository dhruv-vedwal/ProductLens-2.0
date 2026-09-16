/** Intro templates — keep ids in sync with `polishStudio.INTRO_TEMPLATES`. */

export type IntroTemplateId =
  | "aurora_split"
  | "editorial"
  | "noir_browser"
  | "slate_glass"
  | "mist_soft"
  | "studio_cream"
  | "ocean_arc"
  | "ember_ink";

export type IntroTemplateOption = {
  id: IntroTemplateId;
  label: string;
  hint: string;
  /** Fallback / base fill for the Create demo preview card */
  previewBg: string;
  /** Soft moving orbs — similar hues that drift like a short loop */
  previewMesh: [string, string, string];
  dark: boolean;
  frameStyle: "rounded" | "browser" | "minimal";
  accent: string;
};

export const INTRO_TEMPLATES: IntroTemplateOption[] = [
  {
    id: "aurora_split",
    label: "Aurora split",
    hint: "Mist canvas · framed walkthrough",
    previewBg: "linear-gradient(145deg, #120a24 0%, #0a2a40 50%, #1c1038 100%)",
    previewMesh: ["#3d1f6e", "#0e6a8a", "#5a2a7a"],
    dark: true,
    frameStyle: "rounded",
    accent: "#e8c56a",
  },
  {
    id: "editorial",
    label: "Editorial",
    hint: "Cream paper · framed product · serif",
    previewBg: "linear-gradient(160deg, #f6f1ea 0%, #ebe3d8 100%)",
    previewMesh: ["#f0ddd0", "#e4d5c4", "#dcc8b4"],
    dark: false,
    frameStyle: "minimal",
    accent: "#c45c3e",
  },
  {
    id: "noir_browser",
    label: "Noir browser",
    hint: "Ink black · browser frame throughout",
    previewBg: "linear-gradient(160deg, #070707 0%, #141414 100%)",
    previewMesh: ["#2a2a2a", "#1a1a1a", "#3a3030"],
    dark: true,
    frameStyle: "browser",
    accent: "#d4af6a",
  },
  {
    id: "slate_glass",
    label: "Slate glass",
    hint: "Cool slate · framed walkthrough",
    previewBg: "linear-gradient(145deg, #0e141c 0%, #1c2838 100%)",
    previewMesh: ["#2a4058", "#1a3048", "#3a5068"],
    dark: true,
    frameStyle: "rounded",
    accent: "#9ec5e8",
  },
  {
    id: "mist_soft",
    label: "Mist soft",
    hint: "Stone mist · framed for the whole cut",
    previewBg: "linear-gradient(160deg, #f4f4f2 0%, #e8e8e4 100%)",
    previewMesh: ["#e4e0dc", "#d8d4d0", "#ece8e4"],
    dark: false,
    frameStyle: "rounded",
    accent: "#e11d2e",
  },
  {
    id: "studio_cream",
    label: "Studio cream",
    hint: "Warm cream · browser frame throughout",
    previewBg: "linear-gradient(160deg, #f8f2ea 0%, #efe4d6 100%)",
    previewMesh: ["#f0dcc8", "#e8d0b8", "#f5e6d4"],
    dark: false,
    frameStyle: "browser",
    accent: "#b85c38",
  },
  {
    id: "ocean_arc",
    label: "Ocean arc",
    hint: "Deep teal · framed cinematic cut",
    previewBg: "linear-gradient(135deg, #041820 0%, #0a4858 100%)",
    previewMesh: ["#0a6878", "#086070", "#148898"],
    dark: true,
    frameStyle: "rounded",
    accent: "#7ec8c8",
  },
  {
    id: "ember_ink",
    label: "Ember ink",
    hint: "Warm dark · framed ember cut",
    previewBg: "linear-gradient(135deg, #120808 0%, #2a1410 100%)",
    previewMesh: ["#5a2818", "#3a1810", "#6a3020"],
    dark: true,
    frameStyle: "minimal",
    accent: "#e88860",
  },
];

export const DEFAULT_INTRO_TEMPLATE: IntroTemplateId = "aurora_split";
