import React from "react";
import { AbsoluteFill, Easing, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { theme } from "../theme";
import { Backdrop, Watermark, fadeInOut } from "../components";
import { overviewItems } from "../script";

const ease = { easing: Easing.bezier(0.3, 0, 0.2, 1), extrapolateLeft: "clamp" as const, extrapolateRight: "clamp" as const };

export const Overview: React.FC<{ duration: number }> = ({ duration }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const heading = interpolate(frame, [6, 30], [0, 1], ease);
  const note = interpolate(frame, [duration - 130, duration - 100], [0, 1], ease);

  return (
    <AbsoluteFill style={{ opacity: fadeInOut(frame, duration) }}>
      <Backdrop>
        <div style={{ position: "absolute", left: 96, top: 118, opacity: heading }}>
          <div style={{ fontFamily: theme.mono, fontSize: 23, letterSpacing: ".34em", color: theme.lime, marginBottom: 18 }}>PRODUCTION FLOW</div>
          <div style={{ fontFamily: theme.serif, fontSize: 66, color: theme.paper }}>制作流程总览</div>
        </div>

        <div style={{ position: "absolute", left: 96, right: 96, top: 288, display: "flex", flexDirection: "column", gap: 11 }}>
          {overviewItems.map(([index, name, note2], i) => {
            const start = 34 + i * 26;
            const pop = spring({ frame: frame - start, fps, config: { damping: 200, mass: 0.6 } });
            return (
              <div
                key={index}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 30,
                  padding: "16px 32px",
                  borderRadius: 14,
                  background: `linear-gradient(90deg, rgba(216,240,107,${0.1 * pop}) 0%, rgba(13,32,25,.82) 46%)`,
                  border: "1px solid rgba(216,240,107,.16)",
                  opacity: pop,
                  transform: `translateX(${(1 - pop) * 60}px)`,
                }}
              >
                <span style={{ fontFamily: theme.mono, fontSize: 30, fontWeight: 700, color: theme.lime, width: 58 }}>{index}</span>
                <span style={{ fontFamily: theme.sans, fontSize: 32, fontWeight: 700, color: theme.paper, width: 224 }}>{name}</span>
                <span style={{ fontFamily: theme.sans, fontSize: 25, color: "rgba(231,239,233,.62)" }}>{note2}</span>
                <span style={{ marginLeft: "auto", fontFamily: theme.mono, fontSize: 20, color: "rgba(111,211,165,.85)" }}>
                  {i < 5 ? "需人工批准" : i === 5 ? "人声对齐" : "导出成片"}
                </span>
              </div>
            );
          })}
        </div>

        <div
          style={{
            position: "absolute",
            left: 96,
            bottom: 52,
            display: "flex",
            alignItems: "center",
            gap: 16,
            opacity: note,
          }}
        >
          <span style={{ width: 12, height: 12, borderRadius: 3, background: theme.lime, transform: "rotate(45deg)" }} />
          <span style={{ fontFamily: theme.sans, fontSize: 30, color: theme.cream }}>
            侧栏就是这条流程；每一步产出都能人工改，批准后才进入下一步
          </span>
        </div>

        <Watermark />
      </Backdrop>
    </AbsoluteFill>
  );
};
