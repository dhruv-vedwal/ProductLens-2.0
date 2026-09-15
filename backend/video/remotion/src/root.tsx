import React from 'react';
import { AbsoluteFill, Audio, Composition, Easing, interpolate, Sequence, staticFile, useCurrentFrame, Video } from 'remotion';

export type Beat = { eventId?: string; start: number; end: number; clickFrame?: number; intent: string; kind: string; x: number; y: number; width: number; height: number; zoom: number };
export type Caption = { start: number; end: number; text: string };
export type Scene = { event_id: string; show_cursor: boolean; caption_safe_zone: string; story_phase?: string; page_stage?: string; required_content_groups?: string[]; action_class?: string; cursor?: { visible: boolean; hover_seconds: number; click_ripple: boolean }; scroll?: { mode: string; settle_seconds: number } };
export type CursorPath = { event_id: string; source: { x: number; y: number }; destination: { x: number; y: number }; waypoints?: { x: number; y: number }[]; travel_seconds: number; hover_seconds: number; settle_seconds?: number; easing?: string; cursor_style?: string; click: boolean; drag?: boolean };
export type Redaction = { eventId: string; start: number; end: number; mode: 'target-mask' | 'secure-full-frame'; x?: number; y?: number; width?: number; height?: number; label: string };
export type DemoProps = { title: string; subtitle: string; outroTitle?: string; outroSubtitle?: string; screenVideo: string; sourceWidth: number; sourceHeight: number; screenFrames: number; frameRate?: number; playbackRate?: number; beats: Beat[]; cursorPaths?: CursorPath[]; narration?: string | null; captions?: Caption[]; scenes?: Scene[]; redactions?: Redaction[] };

const Intro: React.FC<{ title: string; subtitle: string }> = ({ title, subtitle }) => <AbsoluteFill style={{ background: '#0b1220', color: '#fff', justifyContent: 'center', alignItems: 'center', fontFamily: 'Inter,Arial,sans-serif' }}><div style={{ width: 1120 }}><div style={{ fontSize: 15, letterSpacing: 4, color: '#8bdcff', fontWeight: 700 }}>PRODUCTLENS</div><h1 style={{ fontSize: 48, lineHeight: 1.1, margin: '18px 0' }}>{title}</h1><p style={{ fontSize: 22, color: '#d4e9f6' }}>{subtitle}</p></div></AbsoluteFill>;

const DirectedCursor: React.FC<{ x: number; y: number }> = ({ x, y }) => <div aria-label="cursor" style={{ position: 'absolute', left: x - 4, top: y - 4, width: 30, height: 38, filter: 'drop-shadow(0 2px 3px rgba(0,0,0,.68))', pointerEvents: 'none', zIndex: 10 }}>
  <svg viewBox="0 0 30 38" width="30" height="38"><path d="M3 2 L3 30 L10.2 23.2 L15.2 35 L20.2 32.8 L15.1 21.1 L27 20.9 Z" fill="#fff" stroke="#111827" strokeWidth="2" strokeLinejoin="round" /></svg>
</div>;

const BrowserMotion: React.FC<{ video: string; sourceWidth: number; sourceHeight: number; frameRate: number; playbackRate: number; beats: Beat[]; cursorPaths: CursorPath[]; captions: Caption[]; scenes: Scene[]; redactions: Redaction[] }> = ({ video, sourceWidth, sourceHeight, frameRate, playbackRate, beats, cursorPaths, captions, scenes, redactions }) => {
  const frame = useCurrentFrame();
  const matchingBeat = beats.findIndex(item => frame >= item.start && frame < item.end);
  // Before the first recorded action, keep the opening camera/cursor state.
  // Falling through to the final beat made the opening inherit a later page's
  // camera target, which could visibly warp the otherwise stable home frame.
  const beatIndex = matchingBeat === -1
    ? (frame < (beats[0]?.start ?? 0) ? 0 : Math.max(0, beats.length - 1))
    : matchingBeat;
  const beat = beats[beatIndex] ?? beats[beats.length - 1];
  const previous = beats[Math.max(0, beatIndex - 1)] ?? beat;
  const transitionFrames = Math.min(18, Math.max(1, (beat?.end ?? 1) - (beat?.start ?? 0)));
  const transition = interpolate(frame - (beat?.start ?? 0), [0, transitionFrames], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic) });
  const fallbackX = interpolate(transition, [0, 1], [previous?.x ?? 960, beat?.x ?? 960]);
  const fallbackY = interpolate(transition, [0, 1], [previous?.y ?? 540, beat?.y ?? 540]);
  const cursorPath = cursorPaths.find(item => item.event_id === beat?.eventId);
  const hoverFrames = Math.max(1, Math.round((cursorPath?.hover_seconds ?? .25) * frameRate));
  const travelFrames = Math.max(1, Math.round((cursorPath?.travel_seconds ?? .3) * frameRate));
  const moveEnd = (beat?.clickFrame ?? frame) - hoverFrames;
  const moveStart = Math.max(beat?.start ?? 0, moveEnd - travelFrames);
  const cursorProgress = interpolate(frame, [moveStart, Math.max(moveStart + 1, moveEnd)], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic) });
  // Follow every observed waypoint. A single quadratic bend is retained for
  // legacy paths, while pointer/drag gestures can now preserve a multi-point
  // browser path without teleporting through intermediate targets.
  const pathPoint = (t: number, axis: 'x' | 'y') => {
    if (!cursorPath) return axis === 'x' ? fallbackX : fallbackY;
    const points = [cursorPath.source, ...(cursorPath.waypoints ?? []), cursorPath.destination];
    if (points.length <= 2) return interpolate(t, [0, 1], [points[0][axis], points[1][axis]]);
    const scaled = Math.min(points.length - 1, Math.max(0, t * (points.length - 1)));
    const segment = Math.min(points.length - 2, Math.floor(scaled));
    const local = scaled - segment;
    return interpolate(local, [0, 1], [points[segment][axis], points[segment + 1][axis]]);
  };
  const x = cursorPath ? pathPoint(cursorProgress, 'x') : fallbackX;
  const y = cursorPath ? pathPoint(cursorProgress, 'y') : fallbackY;
  const captionTime = frame / frameRate;
  const activeCaptionIndex = captions.findIndex(item => captionTime >= item.start && captionTime < item.end);
  const fadingCaptionIndex = activeCaptionIndex === -1
    ? captions.findIndex(item => captionTime >= item.end && captionTime < item.end + 0.18)
    : activeCaptionIndex;
  const activeCaption = fadingCaptionIndex >= 0 ? captions[fadingCaptionIndex] : undefined;
  const activeScene = scenes.find(item => item.event_id === beat?.eventId) ?? scenes[beatIndex];
  // Scene plans normally choose this safe zone during Python planning. Keep
  // a render-time geometry guard as well so a stale/recovered scene artifact
  // cannot place a destination-page explanation over dense lower rows. The
  // decision uses only the recorded target geometry and semantic scene role,
  // making trace-only repairs safe for arbitrary products.
  const targetIntroducesDenseContent = Boolean(
    beat && activeScene && (
      // A navigation caption is spoken over the destination page, so it is
      // safer at the top even though the clicked tab itself is tiny.
      activeScene.story_phase === 'enter' ||
      ((activeScene.page_stage === 'explain' || activeScene.page_stage === 'inspect') &&
        beat.y <= sourceHeight * 0.28 &&
        (beat.width >= sourceWidth * 0.22 || Boolean(activeScene.required_content_groups?.length)))
    ),
  );
  // A planner-selected safe zone is a useful default, but it can become
  // unsafe after a responsive layout or a recovered target geometry changes.
  // Keep captions away from the active evidence region at render time: a
  // top caption over a header/navigation target reads as a covered frame,
  // while a bottom caption over a form/result hides the proof being discussed.
  const preferredCaptionAtTop = activeScene?.caption_safe_zone === 'top' || targetIntroducesDenseContent;
  const targetY = beat?.y ?? sourceHeight / 2;
  const captionAtTop = preferredCaptionAtTop
    ? targetY >= sourceHeight * 0.34
    : targetY > sourceHeight * 0.68;
  const captionFadeSeconds = 0.18;
  const captionOpacity = activeCaption ? Math.min(
    1,
    Math.max(0, (captionTime - activeCaption.start) / captionFadeSeconds),
    Math.max(0, (activeCaption.end - captionTime) / captionFadeSeconds),
  ) : 0;
  // Keep the complete source frame inside a restrained presentation shell,
  // using nearly the full canvas so the actual product remains legible. The
  // bounds reserve only a small safety margin; captions live above the frame
  // edge and no browser chrome is cropped at native scale.
  const sourceScale = Math.min((1920 - 32) / sourceWidth, (1080 - 64) / sourceHeight);
  const videoLeft = (1920 - sourceWidth * sourceScale) / 2;
  const videoTop = 32 + (1016 - sourceHeight * sourceScale) / 2;
  // Camera decisions are target-relative, bounded, and interpolated per beat.
  // Keep the source frame at native scale by default; restrained zoom is only
  // applied when the storyboard explicitly requests it.
  const previousZoom = previous?.zoom ?? 1;
  const zoom = interpolate(transition, [0, 1], [previousZoom, beat?.zoom ?? 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const previousFocusX = videoLeft + (previous?.x ?? sourceWidth / 2) * sourceScale;
  const previousFocusY = videoTop + (previous?.y ?? sourceHeight / 2) * sourceScale;
  const focusX = interpolate(transition, [0, 1], [previousFocusX, videoLeft + (beat?.x ?? sourceWidth / 2) * sourceScale], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const focusY = interpolate(transition, [0, 1], [previousFocusY, videoTop + (beat?.y ?? sourceHeight / 2) * sourceScale], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  // At native scale there is no translation.  As zoom increases, move the
  // selected evidence toward the composition centre, then clamp to the full
  // frame bounds so browser chrome and page edges are never over-panned.
  const rawTranslateX = (960 - focusX) * (zoom - 1);
  const rawTranslateY = (540 - focusY) * (zoom - 1);
  const scaledWidth = sourceWidth * sourceScale * zoom;
  const scaledHeight = sourceHeight * sourceScale * zoom;
  // Translation is bounded inside the browser viewport, not against the
  // presentation canvas.  The old canvas-relative bounds let a zoomed source
  // escape its shell, exposing a cut frame/edge of the surrounding layout.
  const frameWidth = sourceWidth * sourceScale;
  const frameHeight = sourceHeight * sourceScale;
  const translateX = Math.min(0, Math.max(frameWidth - scaledWidth, rawTranslateX));
  const translateY = Math.min(0, Math.max(frameHeight - scaledHeight, rawTranslateY));
  // Apply the same transform as the source video: the letterbox offset is an
  // already-composited origin and must not itself be multiplied by zoom.
  // Multiplying ``videoLeft``/``videoTop`` again made the pointer drift away
  // from real click targets on non-16:9 captures and during focus moves.
  const cursorLeft = videoLeft + x * sourceScale * zoom + translateX;
  const cursorTop = videoTop + y * sourceScale * zoom + translateY;
  const clickKinds = new Set(['Click', 'OpenNavigationItem', 'OpenModal', 'CloseModal', 'Submit', 'ApplyFilter', 'Check', 'Uncheck', 'ChooseRadio', 'SelectOption']);
  const clickAge = frame - (beat?.clickFrame ?? (beat?.start ?? frame) + transitionFrames);
  const clickActive = Boolean(beat && clickKinds.has(beat.kind) && clickAge >= 0 && clickAge < 15);
  const rippleSize = interpolate(clickAge, [0, 14], [14, 66], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const rippleOpacity = interpolate(clickAge, [0, 14], [.9, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const activeRedactions = redactions.filter(item => frame >= item.start && frame < item.end);
  return <AbsoluteFill style={{ background: 'radial-gradient(circle at 50% -10%, #243250 0%, #101827 43%, #080b12 100%)', overflow: 'hidden', fontFamily: 'Inter,Arial,sans-serif' }}>
    <div aria-hidden style={{ position: 'absolute', left: videoLeft - 2, top: videoTop - 2, width: sourceWidth * sourceScale + 4, height: sourceHeight * sourceScale + 4, borderRadius: 16, background: '#0a0e17', border: '1px solid rgba(255,255,255,.22)', boxShadow: '0 26px 62px rgba(0,0,0,.42)', overflow: 'hidden' }} />
    {video ? <div aria-label="browser-frame-viewport" style={{ position: 'absolute', zIndex: 1, left: videoLeft, top: videoTop, width: frameWidth, height: frameHeight, borderRadius: 16, overflow: 'hidden' }}><Video src={staticFile(video)} playbackRate={playbackRate} style={{ position: 'absolute', left: 0, top: 0, width: frameWidth, height: frameHeight, objectFit: 'fill', transform: `translate(${translateX}px, ${translateY}px) scale(${zoom})`, transformOrigin: '0 0' }} /></div> : null}
    {activeRedactions.map(item => item.mode === 'secure-full-frame'
      ? <AbsoluteFill key={item.eventId} aria-label={item.label} style={{ zIndex: 8, background: 'linear-gradient(135deg, #0c1526, #132947)', color: '#e8f4ff', display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}><div style={{ textAlign: 'center' }}><div style={{ fontSize: 44, marginBottom: 12 }}>⌁</div><div style={{ fontSize: 28, fontWeight: 700 }}>{item.label}</div><div style={{ marginTop: 9, color: '#b9d8ee', fontSize: 18 }}>Credentials are never shown in the demo.</div></div></AbsoluteFill>
      : <div key={item.eventId} aria-label={item.label} style={{ position: 'absolute', zIndex: 8, left: videoLeft + (item.x ?? 0) * sourceScale * zoom + translateX, top: videoTop + (item.y ?? 0) * sourceScale * zoom + translateY, width: (item.width ?? 0) * sourceScale * zoom, height: (item.height ?? 0) * sourceScale * zoom, borderRadius: 5, background: 'rgba(255,255,255,.985)', color: '#4b5563', border: '1px solid rgba(71,85,105,.28)', display: 'flex', alignItems: 'center', paddingLeft: 14, boxSizing: 'border-box', fontSize: Math.max(13, 17 * sourceScale * zoom), fontWeight: 600, letterSpacing: .2, overflow: 'hidden', pointerEvents: 'none' }}><span>••••••••</span></div>)}
    {beat && activeScene?.show_cursor !== false && activeScene?.cursor?.visible !== false && <>{clickActive && activeScene?.cursor?.click_ripple !== false && <div aria-hidden style={{ position: 'absolute', left: Math.max(10, Math.min(1890, cursorLeft)) - rippleSize / 2, top: Math.max(10, Math.min(1050, cursorTop)) - rippleSize / 2, width: rippleSize, height: rippleSize, borderRadius: '50%', border: '3px solid #fff', boxShadow: '0 0 14px rgba(0,0,0,.7)', opacity: rippleOpacity, pointerEvents: 'none' }} />}<DirectedCursor x={Math.max(10, Math.min(1890, cursorLeft))} y={Math.max(10, Math.min(1050, cursorTop))} /></>}
    {activeCaption && <div style={{ position: 'absolute', zIndex: 20, left: '20%', right: '20%', ...(captionAtTop ? { top: 22 } : { bottom: 22 }), display: 'flex', justifyContent: 'center', pointerEvents: 'none', opacity: captionOpacity }}><div style={{ maxWidth: 900, background: 'rgba(5,9,16,.92)', border: '1px solid rgba(255,255,255,.2)', borderRadius: 14, boxShadow: '0 10px 28px rgba(0,0,0,.38)', padding: '11px 19px 12px', textAlign: 'center', color: '#fff' }}><div style={{ fontSize: 22, lineHeight: 1.28, fontWeight: 650, letterSpacing: .08, textShadow: '0 1px 2px #000' }}>{activeCaption.text}</div></div></div>}
  </AbsoluteFill>;
};

const Outro: React.FC<{ title: string; subtitle: string }> = ({ title, subtitle }) => <AbsoluteFill style={{ background: '#0b1220', color: '#fff', justifyContent: 'center', alignItems: 'center', fontFamily: 'Inter,Arial,sans-serif' }}><div style={{ textAlign: 'center', maxWidth: 1100 }}><div style={{ fontSize: 34, fontWeight: 700 }}>{title}</div>{subtitle && <p style={{ fontSize: 19, color: '#d4e9f6', lineHeight: 1.35 }}>{subtitle}</p>}</div></AbsoluteFill>;
const Demo: React.FC<DemoProps> = (props) => <AbsoluteFill><Sequence durationInFrames={45}><Intro title={props.title} subtitle={props.subtitle} /></Sequence><Sequence from={45} durationInFrames={props.screenFrames}><BrowserMotion video={props.screenVideo} sourceWidth={props.sourceWidth} sourceHeight={props.sourceHeight} frameRate={props.frameRate ?? 30} playbackRate={props.playbackRate ?? 1} beats={props.beats} cursorPaths={props.cursorPaths ?? []} captions={props.captions ?? []} scenes={props.scenes ?? []} redactions={props.redactions ?? []} />{props.narration && <Audio src={staticFile(props.narration)} />}</Sequence><Sequence from={45 + props.screenFrames} durationInFrames={30}><Outro title={props.outroTitle ?? 'Walkthrough complete'} subtitle={props.outroSubtitle ?? ''} /></Sequence></AbsoluteFill>;
export const Root: React.FC = () => <Composition id="ProductLensDemo" component={Demo} width={1920} height={1080} fps={30} durationInFrames={300} defaultProps={{ title: 'Product demo', subtitle: '', outroTitle: 'Walkthrough complete', outroSubtitle: '', screenVideo: '', sourceWidth: 1920, sourceHeight: 1080, screenFrames: 120, frameRate: 30, playbackRate: 1, beats: [], cursorPaths: [], narration: null, captions: [], redactions: [] }} calculateMetadata={({ props }) => ({ fps: props.frameRate ?? 30, durationInFrames: 75 + props.screenFrames })} />;
