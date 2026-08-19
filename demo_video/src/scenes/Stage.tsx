import React from "react";
import { AbsoluteFill, Easing, Img, interpolate, staticFile, useCurrentFrame } from "remotion";
import { theme } from "../theme";
import { Backdrop, Bullets, ProgressRail, Watermark, fadeInOut } from "../components";
import type { StageSpec } from "../script";

const ease = { easing: Easing.bezier(0.3, 0, 0.2, 1), extrapolateLeft: "clamp" as const, extrapolateRight: "clamp" as const };

// Each stage plays in two halves: first the narration panel over a wide, dimmed
// view of the real UI, then a zoom tour that pushes into the controls being
// described (panel steps aside so nothing covers the UI).
export const Stage: React.FC<{ spec: StageSpec; order: number }> = ({ spec, order }) => {
  const frame = useCurrentFrame();
  const duration = spec.durationInFrames;
  const count = spec.plates.length;
  const tourStart = Math.round(duration * 0.52);
  const tour = (duration - tourStart) / count;
  const drift = interpolate(frame, [0, duration], [1.025, 1.065], ease);

  const plates = spec.plates.map((plate, index) => {
    const start = tourStart + index * tour;
    const first = index === 0;
    const last = index === count - 1;
    const windowStart = first ? 0 : start - 16;
    const windowEnd = last ? duration : start + tour + 16;
    const visible = interpolate(
      frame,
      [windowStart, windowStart + 18, windowEnd - 18, windowEnd],
      [first ? 1 : 0, 1, 1, last ? 1 : 0],
      ease,
    );

    const marker = plate.focus
      ? interpolate(frame, [start, start + 16, start + tour * 0.34, start + tour * 0.46], [0, 1, 1, 0], ease)
      : 0;
    const zoom = plate.focus
      ? interpolate(frame, [start + tour * 0.24, start + tour * 0.46, start + tour * 0.86, start + tour * 0.98], [0, 1, 1, 0], ease)
      : 0;
    const target = plate.focus ? Math.min(2.45, Math.min(1 / plate.focus.w, 1 / plate.focus.h)) : 1;
    const cx = plate.focus ? plate.focus.x + plate.focus.w / 2 : 0.5;
    const cy = plate.focus ? plate.focus.y + plate.focus.h / 2 : 0.5;

    return {
      plate,
      visible,
      marker,
      zoom,
      scale: drift * (1 + (target - 1) * zoom),
      tx: (0.5 - cx) * 100 * zoom,
      ty: (0.5 - cy) * 100 * zoom,
    };
  });

  // The panel clears out just before the tour begins, so highlights never sit
  // under the narration text.
  const panelHide = interpolate(frame, [tourStart - 34, tourStart - 4], [0, 1], ease);
  const attention = Math.max(panelHide, ...plates.map((item) => Math.max(item.zoom, item.marker * 0.94)));
  const spotlight = plates.reduce((best, item) => (Math.max(item.zoom, item.marker) > Math.max(best.zoom, best.marker) ? item : best), plates[0]);
  const bulletStagger = Math.max(30, (tourStart - 96) / Math.max(1, spec.bullets.length));

  return (
    <AbsoluteFill style={{ opacity: fadeInOut(frame, duration) }}>
      <Backdrop>
        <AbsoluteFill>
          {plates.map((item, index) => (
            <AbsoluteFill key={index} style={{ opacity: item.visible }}>
              <AbsoluteFill
                style={{
                  transform: `scale(${item.scale}) translate(${item.tx}%, ${item.ty}%)`,
                  filter: `brightness(${0.5 + 0.5 * Math.max(panelHide, item.zoom)}) saturate(${0.86 + 0.14 * item.zoom})`,
                }}
              >
                <Img src={staticFile(item.plate.src)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
              </AbsoluteFill>
              {item.plate.focus && item.marker > 0.01 ? (
                <div
                  style={{
                    position: "absolute",
                    left: `${item.plate.focus.x * 100}%`,
                    top: `${item.plate.focus.y * 100}%`,
                    width: `${item.plate.focus.w * 100}%`,
                    height: `${item.plate.focus.h * 100}%`,
                    border: `3px solid ${theme.lime}`,
                    borderRadius: 16,
                    boxShadow: `0 0 0 9999px rgba(4,12,9,${0.52 * item.marker}), 0 0 40px rgba(216,240,107,.55)`,
                    opacity: item.marker,
                  }}
                />
              ) : null}
            </AbsoluteFill>
          ))}
        </AbsoluteFill>

        <AbsoluteFill
          style={{
            background: "linear-gradient(100deg, rgba(6,17,12,.97) 0%, rgba(6,17,12,.93) 36%, rgba(6,17,12,.4) 60%, rgba(6,17,12,.12) 100%)",
            opacity: 1 - attention,
          }}
        />

        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            top: 0,
            height: 138,
            background: "linear-gradient(180deg, rgba(5,14,10,.96) 0%, rgba(5,14,10,.55) 62%, transparent 100%)",
          }}
        />
        <ProgressRail active={order} />

        <div
          style={{
            position: "absolute",
            left: 96,
            top: 214,
            width: 720,
            opacity: 1 - panelHide,
            transform: `translateX(${-panelHide * 48}px)`,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 18, marginBottom: 22 }}>
            <span
              style={{
                fontFamily: theme.mono,
                fontSize: 22,
                fontWeight: 700,
                color: theme.ink,
                background: theme.lime,
                padding: "7px 14px",
                borderRadius: 10,
                letterSpacing: ".1em",
              }}
            >
              {spec.index}
            </span>
            <span style={{ fontFamily: theme.sans, fontSize: 26, letterSpacing: ".28em", color: theme.lime }}>{spec.name}</span>
          </div>
          <div style={{ fontFamily: theme.serif, fontSize: 62, lineHeight: 1.24, color: theme.paper, marginBottom: 18 }}>{spec.headline}</div>
          <div style={{ fontFamily: theme.sans, fontSize: 25, lineHeight: 1.6, color: "rgba(231,239,233,.66)", marginBottom: 42 }}>{spec.purpose}</div>
          <Bullets items={spec.bullets} from={46} stagger={bulletStagger} dim={0} />
        </div>

        {spotlight.plate.focus ? (
          <div
            style={{
              position: "absolute",
              left: 96,
              bottom: 88,
              display: "flex",
              alignItems: "center",
              gap: 16,
              padding: "20px 32px",
              borderRadius: 16,
              background: "rgba(6,17,12,.94)",
              border: `1px solid rgba(216,240,107,.42)`,
              boxShadow: "0 22px 60px rgba(0,0,0,.45)",
              opacity: Math.max(spotlight.zoom, spotlight.marker),
            }}
          >
            <span style={{ width: 12, height: 12, borderRadius: 3, background: theme.lime, transform: "rotate(45deg)" }} />
            <span style={{ fontFamily: theme.sans, fontSize: 30, color: theme.paper }}>{spotlight.plate.focus.label}</span>
          </div>
        ) : null}

        <Watermark />
      </Backdrop>
    </AbsoluteFill>
  );
};
