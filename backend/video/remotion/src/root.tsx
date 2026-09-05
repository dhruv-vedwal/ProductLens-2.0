import React from 'react';
import { AbsoluteFill, Audio, Composition, Easing, interpolate, OffthreadVideo, Sequence, staticFile, useCurrentFrame } from 'remotion';

export type Beat = { eventId?: string; start: number; end: number; clickFrame?: number; intent: string; kind: string; x: number; y: number; width: number; height: number; zoom: number };
export type Caption = { start: number; end: number; text: string };
export type Scene = { event_id: string; show_cursor: boolean; caption_safe_zone: string; story_phase?: string; action_class?: string; cursor?: { visible: boolean; hover_seconds: number; click_ripple: boolean }; scroll?: { mode: string; settle_seconds: number } };
export type CursorPath = { event_id: string; source: { x: number; y: number }; destination: { x: number; y: number }; waypoints?: { x: number; y: number }[]; travel_seconds: number; hover_seconds: number; settle_seconds?: number; easing?: string; cursor_style?: string; click: boolean };
export type DemoProps = { title: string; subtitle: string; screenVideo: string; sourceWidth: number; sourceHeight: number; screenFrames: number; frameRate?: number; playbackRate?: number; beats: Beat[]; cursorPaths?: CursorPath[]; narration?: string | null; captions?: Caption[]; scenes?: Scene[] };

const Intro: React.FC<{ title: string; subtitle: string }> = ({ title, subtitle }) => <AbsoluteFill style={{ background: '#0b1220', color: '#fff', justifyContent: 'center', alignItems: 'center', fontFamily: 'Inter,Arial,sans-serif' }}><div style={{ width: 1120 }}><div style={{ fontSize: 15, letterSpacing: 4, color: '#8bdcff', fontWeight: 700 }}>PRODUCTLENS</div><h1 style={{ fontSize: 48, lineHeight: 1.1, margin: '18px 0' }}>{title}</h1><p style={{ fontSize: 22, color: '#d4e9f6' }}>{subtitle}</p></div></AbsoluteFill>;

const DirectedCursor: React.FC<{ x: number; y: number }> = ({ x, y }) => <div aria-label="cursor" style={{ position: 'absolute', left: x - 4, top: y - 4, width: 30, height: 38, filter: 'drop-shadow(0 2px 3px rgba(0,0,0,.68))', pointerEvents: 'none', zIndex: 10 }}>
  <svg viewBox="0 0 30 38" width="30" height="38"><path d="M3 2 L3 30 L10.2 23.2 L15.2 35 L20.2 32.8 L15.1 21.1 L27 20.9 Z" fill="#fff" stroke="#111827" strokeWidth="2" strokeLinejoin="round" /></svg>
</div>;

const BrowserMotion: React.FC<{ video: string; sourceWidth: number; sourceHeight: number; frameRate: number; playbackRate: number; beats: Beat[]; cursorPaths: CursorPath[]; captions: Caption[]; scenes: Scene[] }> = ({ video, sourceWidth, sourceHeight, frameRate, playbackRate, beats, cursorPaths, captions, scenes }) => {
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
  // A bounded quadratic path feels human while its final point remains the
  // captured target centre. Existing props without a waypoint retain a
  // straight path for backwards-compatible renders.
  const waypoint = cursorPath?.waypoints?.[0];
  const curve = cursorPath && waypoint ? (t: number, axis: 'x' | 'y') => {
    const start = cursorPath.source[axis], control = waypoint[axis], end = cursorPath.destination[axis];
    return (1 - t) * (1 - t) * start + 2 * (1 - t) * t * control + t * t * end;
  } : undefined;
  const x = curve ? curve(cursorProgress, 'x') : cursorPath ? interpolate(cursorProgress, [0, 1], [cursorPath.source.x, cursorPath.destination.x]) : fallbackX;
  const y = curve ? curve(cursorProgress, 'y') : cursorPath ? interpolate(cursorProgress, [0, 1], [cursorPath.source.y, cursorPath.destination.y]) : fallbackY;
  const activeCaption = captions.find(item => frame / frameRate >= item.start && frame / frameRate < item.end);
  const activeScene = scenes.find(item => item.event_id === beat?.eventId) ?? scenes[beatIndex];
  // Keep the source website full-frame. Decorative browser cards and scaling
  // make real product chrome, sidebars, and edge animations disappear.
  const sourceScale = Math.min(1920 / sourceWidth, 1080 / sourceHeight);
  const videoLeft = (1920 - sourceWidth * sourceScale) / 2;
  const videoTop = (1080 - sourceHeight * sourceScale) / 2;
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
  const translateX = Math.min(1920 - videoLeft - scaledWidth, Math.max(-videoLeft, rawTranslateX));
  const translateY = Math.min(1080 - videoTop - scaledHeight, Math.max(-videoTop, rawTranslateY));
  const sourcePointX = videoLeft + x * sourceScale;
  const sourcePointY = videoTop + y * sourceScale;
  const cursorLeft = sourcePointX * zoom + translateX;
  const cursorTop = sourcePointY * zoom + translateY;
  const clickKinds = new Set(['Click', 'OpenNavigationItem', 'OpenModal', 'CloseModal', 'Submit', 'ApplyFilter', 'Check', 'Uncheck', 'ChooseRadio', 'SelectOption']);
  const clickAge = frame - (beat?.clickFrame ?? (beat?.start ?? frame) + transitionFrames);
  const clickActive = Boolean(beat && clickKinds.has(beat.kind) && clickAge >= 0 && clickAge < 15);
  const rippleSize = interpolate(clickAge, [0, 14], [14, 66], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const rippleOpacity = interpolate(clickAge, [0, 14], [.9, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  return <AbsoluteFill style={{ background: '#080b12', overflow: 'hidden', fontFamily: 'Inter,Arial,sans-serif' }}>
    <OffthreadVideo src={staticFile(video)} playbackRate={playbackRate} style={{ position: 'absolute', left: videoLeft, top: videoTop, width: sourceWidth * sourceScale, height: sourceHeight * sourceScale, objectFit: 'fill', transform: `translate(${translateX}px, ${translateY}px) scale(${zoom})`, transformOrigin: '0 0' }} />
    {beat && activeScene?.show_cursor !== false && activeScene?.cursor?.visible !== false && <>{clickActive && activeScene?.cursor?.click_ripple !== false && <div aria-hidden style={{ position: 'absolute', left: Math.max(10, Math.min(1890, cursorLeft)) - rippleSize / 2, top: Math.max(10, Math.min(1050, cursorTop)) - rippleSize / 2, width: rippleSize, height: rippleSize, borderRadius: '50%', border: '3px solid #fff', boxShadow: '0 0 14px rgba(0,0,0,.7)', opacity: rippleOpacity, pointerEvents: 'none' }} />}<DirectedCursor x={Math.max(10, Math.min(1890, cursorLeft))} y={Math.max(10, Math.min(1050, cursorTop))} /></>}
    {activeCaption && <div style={{ position: 'absolute', left: '28%', right: '28%', ...(activeScene?.caption_safe_zone === 'top' ? { top: 18 } : { bottom: 18 }), display: 'flex', justifyContent: 'center', pointerEvents: 'none' }}><div style={{ maxWidth: 720, background: 'rgba(8,12,20,.70)', border: '1px solid rgba(255,255,255,.15)', borderRadius: 9, boxShadow: '0 5px 16px rgba(0,0,0,.24)', padding: '7px 13px 8px', textAlign: 'center', color: '#fff', backdropFilter: 'blur(10px)' }}><div style={{ fontSize: 18, lineHeight: 1.3, fontWeight: 600, letterSpacing: .05, textShadow: '0 1px 2px #000' }}>{activeCaption.text}</div></div></div>}
  </AbsoluteFill>;
};

const Outro: React.FC = () => <AbsoluteFill style={{ background: '#0b1220', color: '#fff', justifyContent: 'center', alignItems: 'center', fontFamily: 'Inter,Arial,sans-serif' }}><div style={{ textAlign: 'center' }}><div style={{ fontSize: 34, fontWeight: 700 }}>Product walkthrough complete</div></div></AbsoluteFill>;
const Demo: React.FC<DemoProps> = (props) => <AbsoluteFill><Sequence durationInFrames={45}><Intro title={props.title} subtitle={props.subtitle} /></Sequence><Sequence from={45} durationInFrames={props.screenFrames}><BrowserMotion video={props.screenVideo} sourceWidth={props.sourceWidth} sourceHeight={props.sourceHeight} frameRate={props.frameRate ?? 30} playbackRate={props.playbackRate ?? 1} beats={props.beats} cursorPaths={props.cursorPaths ?? []} captions={props.captions ?? []} scenes={props.scenes ?? []} />{props.narration && <Audio src={staticFile(props.narration)} />}</Sequence><Sequence from={45 + props.screenFrames} durationInFrames={30}><Outro /></Sequence></AbsoluteFill>;
export const Root: React.FC = () => <Composition id="ProductLensDemo" component={Demo} width={1920} height={1080} fps={30} durationInFrames={300} defaultProps={{ title: 'Product demo', subtitle: '', screenVideo: '', sourceWidth: 1920, sourceHeight: 1080, screenFrames: 120, frameRate: 30, playbackRate: 1, beats: [], cursorPaths: [], narration: null, captions: [] }} calculateMetadata={({ props }) => ({ fps: props.frameRate ?? 30, durationInFrames: 75 + props.screenFrames })} />;
