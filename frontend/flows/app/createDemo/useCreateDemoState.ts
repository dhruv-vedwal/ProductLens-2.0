"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  createCredential,
  createProject,
  createRun,
  listCredentials,
  listProjects,
  previewUnderstanding,
} from "@/flows/app/createDemo/api";
import { ACCENT_LABELS, FALLBACK_OPTIONS } from "@/flows/app/createDemo/constants";
import { EMPTY_PRODUCT_CONTEXT, EMPTY_STEPS } from "@/flows/app/createDemo/defaults";
import { type StoryPicks } from "@/flows/app/createDemo/storySuggestions";
import {
  DEFAULT_INTRO_TEMPLATE,
  type IntroTemplateId,
} from "@/flows/app/createDemo/introTemplates";
import type {
  CredentialItem,
  DemoMode,
  GuidedStep,
  NarrationStyle,
  ProjectSummary,
  UnderstandingPreview,
  VoiceOption,
} from "@/flows/app/createDemo/types";
import { useAuthStore } from "@/flows/auth/store";

export function useCreateDemoState() {
  const token = useAuthStore((s) => s.token);
  const router = useRouter();
  const [stepIndex, setStepIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [projectName, setProjectName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [loginUsername, setLoginUsername] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [picks, setPicks] = useState<StoryPicks>({ audience: [], show: [], tone: [] });
  const [briefTouched, setBriefTouched] = useState(true);
  const [briefing, setBriefing] = useState(false);
  const [context, setContext] = useState(EMPTY_PRODUCT_CONTEXT);
  const [showMoreContext, setShowMoreContext] = useState(false);
  const [mode, setMode] = useState<DemoMode>("goal");
  const [goalText, setGoalText] = useState("");
  const [featureScope, setFeatureScope] = useState("");
  const [showJson, setShowJson] = useState(false);
  const [steps, setSteps] = useState<GuidedStep[]>(EMPTY_STEPS);
  const [planning, setPlanning] = useState(false);
  const [language, setLanguage] = useState("en");
  const [accent, setAccent] = useState("us");
  const [voiceOptionId, setVoiceOptionId] = useState<string | null>(null);
  const [voiceOptions, setVoiceOptions] = useState<VoiceOption[]>(FALLBACK_OPTIONS);
  const [accents, setAccents] = useState<string[]>(["us", "uk", "indian", "neutral"]);
  const [voicesLoading, setVoicesLoading] = useState(false);
  const [pace, setPace] = useState(1);
  const [targetDurationMinutes, setTargetDurationMinutes] = useState(2);
  const [includeAudio, setIncludeAudio] = useState(true);
  const [browserZoomPercent, setBrowserZoomPercent] = useState(100);
  const [subtitlesEnabled, setSubtitlesEnabled] = useState(true);
  const [subtitleStyle, setSubtitleStyle] = useState("minimal");
  const [subtitlePosition, setSubtitlePosition] = useState("bottom");
  const [subtitleFontSize, setSubtitleFontSize] = useState("md");
  const [introTemplate, setIntroTemplate] = useState<IntroTemplateId>(DEFAULT_INTRO_TEMPLATE);
  const [studioPolish, setStudioPolish] = useState(true);
  const [cursorStyle, setCursorStyle] = useState("default");
  const [highlightStyle, setHighlightStyle] = useState("spotlight");
  const [clickZoom, setClickZoom] = useState(true);
  const [narrationStyle, setNarrationStyle] = useState<NarrationStyle>("teach");
  const [previewBeats, setPreviewBeats] = useState<GuidedStep[]>([]);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [filmSummary, setFilmSummary] = useState<string | null>(null);
  const [filmIntent, setFilmIntent] = useState<Record<string, unknown> | null>(null);
  const [droppedChapters, setDroppedChapters] = useState<string[]>([]);
  const [exportAspect, setExportAspect] = useState("16:9");
  const [exportResolution, setExportResolution] = useState("1080");
  const [showLookAdvanced, setShowLookAdvanced] = useState(false);
  const [credentialReference, setCredentialReference] = useState("");
  const [credentials, setCredentials] = useState<CredentialItem[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [matchedProject, setMatchedProject] = useState<ProjectSummary | null>(null);
  const [connectBusy, setConnectBusy] = useState(false);
  const [connectMsg, setConnectMsg] = useState<string | null>(null);
  const [ensureBusy, setEnsureBusy] = useState(false);
  const [understanding, setUnderstanding] = useState<UnderstandingPreview | null>(null);
  const [understandingKey, setUnderstandingKey] = useState<string | null>(null);
  const [cloudDiscovery, setCloudDiscovery] = useState<boolean | null>(null);

  // Compat aliases for ProductStep / ReviewStep craft
  const browserSessionId = credentialReference;
  const setBrowserSessionId = setCredentialReference;
  const browserSessions = credentials.map((c) => ({
    id: c.reference,
    label: c.name,
  }));

  const connectionBadge = useMemo(() => {
    if (credentialReference || (loginUsername && loginPassword)) {
      return { label: "Credentials ready", tone: "ok" as const };
    }
    return { label: "Optional login", tone: "muted" as const };
  }, [credentialReference, loginUsername, loginPassword]);

  function toggleDroppedChapter(title: string) {
    setDroppedChapters((prev) =>
      prev.includes(title) ? prev.filter((t) => t !== title) : [...prev, title],
    );
  }

  const modeLabel =
    mode === "feature"
      ? "Feature"
      : mode === "guided"
        ? "Guided"
        : mode === "autonomous"
          ? "Autonomous"
          : "Goal";

  useEffect(() => {
    if (stepIndex !== 0 || !token) return;
    listCredentials(token)
      .then(setCredentials)
      .catch(() => setCredentials([]));
    listProjects(token)
      .then(setProjects)
      .catch(() => setProjects([]));
  }, [stepIndex, token]);

  useEffect(() => {
    if (!projects.length) {
      setMatchedProject(null);
      return;
    }
    setMatchedProject(projects[0] ?? null);
  }, [projects]);

  useEffect(() => {
    setVoiceOptions(FALLBACK_OPTIONS);
    setAccents(["us", "uk", "indian", "neutral"]);
    setVoicesLoading(false);
  }, [language]);

  const coverageWarning = null;

  function validateStory(): string | null {
    if (!goalText.trim()) return "A task prompt is required";
    return null;
  }

  function togglePick(group: keyof StoryPicks, id: string) {
    const list = picks[group];
    const nextList = list.includes(id) ? list.filter((item) => item !== id) : [...list, id];
    setPicks({ ...picks, [group]: nextList });
  }

  function editBrief(value: string) {
    setGoalText(value);
    setBriefTouched(true);
  }

  async function generateBriefWithAi() {
    if (!token || !baseUrl.trim()) {
      setPreviewError("Add a target URL before asking AI to write the brief.");
      return;
    }
    setPreviewLoading(true);
    setPreviewError(null);
    try {
      const previewKey = `${baseUrl.trim()}\n${goalText.trim()}\n${context.audience.trim() || picks.audience[0] || ""}`;
      const preview = await previewUnderstanding(token, {
        url: baseUrl.trim(),
        prompt: goalText.trim(),
        audience: context.audience.trim() || picks.audience[0] || null,
        max_pages: 3,
      });
      setUnderstanding(preview);
      setUnderstandingKey(previewKey);
      setGoalText(preview.suggested_prompt || goalText);
      setFilmSummary(
        preview.suggested_prompt ||
          (preview.relevant_areas.length
            ? `A grounded walkthrough covering ${preview.relevant_areas.slice(0, 3).join(", ")}.`
            : "A grounded walkthrough of the product's important visible areas."),
      );
      setFilmIntent({
        demo_type: preview.objective?.demo_type,
        primary_entity: preview.objective?.primary_entity,
        relevant_areas: preview.relevant_areas,
        pages_inspected: preview.pages_inspected,
        evidence_refs: preview.evidence_refs,
      });
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : "Could not understand this product yet");
    } finally {
      setPreviewLoading(false);
    }
  }

  async function ensureProject() {
    if (!token) return null;
    if (matchedProject) return matchedProject;
    setEnsureBusy(true);
    try {
      const row = await createProject(token, projectName.trim() || "Demo");
      setProjects((prev) => [row, ...prev.filter((p) => p.id !== row.id)]);
      setMatchedProject(row);
      return row;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save project");
      return null;
    } finally {
      setEnsureBusy(false);
    }
  }

  async function draftWithAi() {
    // Drafting and brief generation share one cached, non-recording
    // understanding call.  This keeps the UI honest and avoids an empty
    // "AI draft" action that used to report success without doing anything.
    await generateBriefWithAi();
  }

  async function goNext() {
    if (stepIndex === 0) {
      if (!baseUrl.trim()) {
        setError("Target URL is required");
        return;
      }
      await ensureProject();
      if (token && baseUrl.trim() && goalText.trim()) {
        setPreviewLoading(true);
        try {
          const previewKey = `${baseUrl.trim()}\n${goalText.trim()}\n${context.audience.trim() || picks.audience[0] || ""}`;
          const preview = await previewUnderstanding(token, {
            url: baseUrl.trim(),
            prompt: goalText.trim(),
            audience: context.audience.trim() || null,
            max_pages: 3,
          });
          setUnderstanding(preview);
          setUnderstandingKey(previewKey);
          setFilmSummary(
            preview.suggested_prompt ||
              `A grounded walkthrough covering ${preview.relevant_areas.slice(0, 3).join(", ") || "the visible product"}.`,
          );
          setFilmIntent({
            demo_type: preview.objective?.demo_type,
            primary_entity: preview.objective?.primary_entity,
            relevant_areas: preview.relevant_areas,
            pages_inspected: preview.pages_inspected,
            evidence_refs: preview.evidence_refs,
          });
        } catch {
          /* preview is optional */
        } finally {
          setPreviewLoading(false);
        }
      }
    }
    if (stepIndex === 1) {
      const storyError = validateStory();
      if (storyError) {
        setError(storyError);
        return;
      }
    }
    setError(null);
    setStepIndex(stepIndex + 1);
  }

  async function onGenerate(e?: FormEvent) {
    e?.preventDefault();
    if (stepIndex !== 2) return;
    if (!token) return;
    if (!baseUrl.trim()) {
      setError("Target URL is required");
      return;
    }
    const storyError = validateStory();
    if (storyError) {
      setError(storyError);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const project = matchedProject || (await ensureProject());
      let credential_reference = credentialReference.trim() || null;

      if (!credential_reference && loginUsername.trim() && loginPassword.trim()) {
        const slug =
          (projectName.trim() || "demo")
            .toLowerCase()
            .replace(/[^a-z0-9_-]+/g, "-")
            .replace(/^-+|-+$/g, "")
            .slice(0, 48) || "demo";
        const created = await createCredential(token, {
          name: `${slug}-${Date.now().toString(36).slice(-4)}`,
          username: loginUsername.trim(),
          password: loginPassword,
          project_id: project?.id ?? null,
        });
        credential_reference = created.reference;
      }

      const targetDurationSeconds = Math.round(
        Math.min(10, Math.max(0.5, targetDurationMinutes)) * 60,
      );

      // Reuse the bounded understanding scan from the first wizard step. If
      // the user edited the URL/prompt afterwards, refresh exactly once rather
      // than issuing an unconditional duplicate model/browser call.
      const previewKey = `${baseUrl.trim()}\n${goalText.trim()}\n${context.audience.trim() || picks.audience[0] || ""}`;
      if (understandingKey !== previewKey) {
        try {
          const preview = await previewUnderstanding(token, {
            url: baseUrl.trim(),
            prompt: goalText.trim(),
            audience: context.audience.trim() || picks.audience[0] || null,
            max_pages: 3,
          });
          setUnderstanding(preview);
          setUnderstandingKey(previewKey);
        } catch {
          /* non-blocking; generation performs its own authoritative discovery */
        }
      }

      const result = await createRun(token, {
        url: baseUrl.trim(),
        objective: goalText.trim(),
        audience:
          context.audience.trim() ||
          picks.audience[0] ||
          "product prospect",
        project_id: project?.id ?? null,
        credential_reference,
        target_duration_seconds: Math.max(30, targetDurationSeconds),
        max_pages: 6,
        render: true,
        cloud_discovery: cloudDiscovery,
        presentation: {
          include_audio: includeAudio,
          narration_style: narrationStyle,
          pace,
          browser_zoom_percent: browserZoomPercent,
          subtitles_enabled: subtitlesEnabled,
          subtitle_style: subtitleStyle as "minimal" | "apple" | "youtube",
          subtitle_position: subtitlePosition as "top" | "bottom",
          subtitle_font_size: subtitleFontSize as "sm" | "md" | "lg",
          intro_template: introTemplate,
          studio_polish: studioPolish,
          cursor_style: cursorStyle,
          highlight_style: highlightStyle,
          click_zoom: clickZoom,
          export_aspect: exportAspect as "16:9" | "9:16" | "1:1",
          export_resolution: exportResolution as "720" | "1080" | "1440" | "2160",
          language,
          accent,
        },
      });
      router.push(`/timeline?run=${result.run_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start run");
    } finally {
      setBusy(false);
    }
  }

  async function onConnect() {
    if (!token) return;
    if (!(loginUsername && loginPassword) && !credentialReference) {
      setConnectMsg("Add a username and password, or pick a saved credential.");
      return;
    }
    setConnectBusy(true);
    setConnectMsg("Saving credential…");
    try {
      if (loginUsername && loginPassword) {
        const slug =
          (projectName.trim() || "demo")
            .toLowerCase()
            .replace(/[^a-z0-9_-]+/g, "-")
            .slice(0, 48) || "demo";
        const created = await createCredential(token, {
          name: `${slug}-${Date.now().toString(36).slice(-4)}`,
          username: loginUsername.trim(),
          password: loginPassword,
          project_id: matchedProject?.id ?? null,
        });
        setCredentialReference(created.reference);
        setCredentials((prev) => [created, ...prev]);
        setConnectMsg("Credential saved — it will be used on generate.");
      } else {
        setConnectMsg("Using selected vault credential.");
      }
    } catch (err) {
      setConnectMsg(err instanceof Error ? err.message : "Could not save credential");
    } finally {
      setConnectBusy(false);
    }
  }

  return {
    token,
    stepIndex,
    setStepIndex,
    busy,
    error,
    setError,
    projectName,
    setProjectName,
    baseUrl,
    setBaseUrl,
    loginUsername,
    setLoginUsername,
    loginPassword,
    setLoginPassword,
    context,
    setContext,
    picks,
    togglePick,
    editBrief,
    generateBriefWithAi,
    briefing,
    setWhatToAvoid: (value: string) => setContext({ ...context, whatToAvoid: value }),
    showMoreContext,
    setShowMoreContext,
    mode,
    setMode,
    goalText,
    setGoalText,
    featureScope,
    setFeatureScope,
    showJson,
    setShowJson,
    steps,
    setSteps,
    planning,
    language,
    setLanguage,
    accent,
    setAccent,
    accents,
    voiceOptionId,
    setVoiceOptionId,
    voiceOptions,
    voicesLoading,
    pace,
    setPace,
    targetDurationMinutes,
    setTargetDurationMinutes,
    includeAudio,
    setIncludeAudio,
    browserZoomPercent,
    setBrowserZoomPercent,
    subtitlesEnabled,
    setSubtitlesEnabled,
    subtitleStyle,
    setSubtitleStyle,
    subtitlePosition,
    setSubtitlePosition,
    subtitleFontSize,
    setSubtitleFontSize,
    introTemplate,
    setIntroTemplate,
    studioPolish,
    setStudioPolish,
    cursorStyle,
    setCursorStyle,
    highlightStyle,
    setHighlightStyle,
    clickZoom,
    setClickZoom,
    narrationStyle,
    setNarrationStyle,
    previewBeats,
    previewLoading,
    previewError,
    filmSummary,
    filmIntent,
    droppedChapters,
    exportAspect,
    setExportAspect,
    exportResolution,
    setExportResolution,
    showLookAdvanced,
    setShowLookAdvanced,
    browserSessionId,
    setBrowserSessionId,
    browserSessions,
    projects,
    matchedProject,
    connectionBadge,
    connectBusy,
    connectMsg,
    ensureBusy,
    coverageWarning,
    goNext,
    onGenerate,
    onConnect,
    draftWithAi,
    ensureProject,
    understanding,
    cloudDiscovery,
    setCloudDiscovery,
    accentLabel: ACCENT_LABELS[accent] || accent,
    modeLabel,
    toggleDroppedChapter,
  };
}

export type CreateDemoState = ReturnType<typeof useCreateDemoState>;
