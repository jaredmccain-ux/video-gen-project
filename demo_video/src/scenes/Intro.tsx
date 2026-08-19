import React from "react";
import { AbsoluteFill, Easing, interpolate, useCurrentFrame } from "remotion";
import { theme } from "../theme";
import { Backdrop, PixelWord, fadeInOut } from "../components";

const ease = { easing: Easing.bezier(0.3, 0, 0.2, 1), extrapolateLeft: "clamp" as const, extrapolateRight: "clamp" as const };

export const Intro: React.FC<{ duration: number }> = ({ duration }) => {
  const frame = useCurrentFrame();
  const eyebrow = interpolate(frame, [10, 34], [0, 1], ease);
  const rule = interpolate(frame, [64, 96], [0, 1], ease);
  const line1 = interpolate(frame, [96, 124], [0, 1], ease);
  const line2 = interpolate(frame, [134, 162], [0, 1], ease);
  const badge = interpolate(frame, [186, 214], [0, 1], ease);

  return (
    <AbsoluteFill style={{ opacity: fadeInOut(frame, duration, 18) }}>
      <Backdrop>
        <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", gap: 40 }}>
          <div
            style={{
              fontFamily: theme.mono,
              fontSize: 25,
              letterSpacing: ".52em",
              color: "rgba(216,240,107,.8)",
              opacity: eyebrow,
              transform: `translateY(${(1 - eyebrow) * 12}px)`,
            }}
          >
            MINIMAX H3 STUDIO
          </div>

          <PixelWord text="SCENEFLOW" pixel={16} appear={{ from: 20, span: 52 }} />

          <div style={{ display: "flex", alignItems: "center", gap: 26, opacity: rule }}>
            <span style={{ width: 150 * rule, height: 1, background: "rgba(216,240,107,.5)" }} />
            <span style={{ fontFamily: theme.sans, fontSize: 27, letterSpacing: ".3em", color: theme.cream }}>短剧生成流水线</span>
            <span style={{ width: 150 * rule, height: 1, background: "rgba(216,240,107,.5)" }} />
          </div>

          <div style={{ marginTop: 26, textAlign: "center" }}>
            <div
              style={{
                fontFamily: theme.serif,
                fontSize: 58,
                color: theme.paper,
                opacity: line1,
                transform: `translateY(${(1 - line1) * 16}px)`,
              }}
            >
              从一张灵感图，到一条带字幕的成片
            </div>
            <div
              style={{
                marginTop: 22,
                fontFamily: theme.sans,
                fontSize: 30,
                color: "rgba(231,239,233,.66)",
                opacity: line2,
                transform: `translateY(${(1 - line2) * 16}px)`,
              }}
            >
              七个阶段，每一步都由人确认后再往下走
            </div>
          </div>

          <div
            style={{
              marginTop: 24,
              display: "flex",
              gap: 14,
              alignItems: "center",
              padding: "14px 28px",
              borderRadius: 999,
              border: "1px solid rgba(216,240,107,.3)",
              background: "rgba(216,240,107,.07)",
              opacity: badge,
            }}
          >
            <span style={{ width: 9, height: 9, borderRadius: 99, background: theme.teal }} />
            <span style={{ fontFamily: theme.mono, fontSize: 22, letterSpacing: ".16em", color: theme.cream }}>
              本片全部界面与镜头，均来自系统真实产出
            </span>
          </div>
        </AbsoluteFill>
      </Backdrop>
    </AbsoluteFill>
  );
};
