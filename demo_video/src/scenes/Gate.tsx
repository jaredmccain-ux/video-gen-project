import React from "react";
import { AbsoluteFill, Easing, Img, interpolate, staticFile, useCurrentFrame } from "remotion";
import { theme } from "../theme";
import { Backdrop, Watermark, fadeInOut } from "../components";
import { gateBullets } from "../script";

const ease = { easing: Easing.bezier(0.3, 0, 0.2, 1), extrapolateLeft: "clamp" as const, extrapolateRight: "clamp" as const };
const TYPED = "start";

// The gate is a terminal, so it gets the whole frame at a readable zoom and the
// narration sits in a strip along the bottom instead of a side panel.
export const Gate: React.FC<{ duration: number }> = ({ duration }) => {
  const frame = useCurrentFrame();
  const scale = interpolate(frame, [0, duration], [1.46, 1.54], ease);
  const badge = interpolate(frame, [8, 32], [0, 1], ease);
  const typedCount = Math.round(interpolate(frame, [duration - 156, duration - 106], [0, TYPED.length], ease));
  const enter = interpolate(frame, [duration - 78, duration - 60], [0, 1], ease);

  return (
    <AbsoluteFill style={{ opacity: fadeInOut(frame, duration) }}>
      <Backdrop>
        <AbsoluteFill style={{ transform: `scale(${scale}) translate(0%, -9.6%)`, filter: "brightness(.96)" }}>
          <Img src={staticFile("shots/gate-a.png")} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
        </AbsoluteFill>

        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            bottom: 0,
            paddingTop: 66,
            paddingBottom: 46,
            background: "linear-gradient(0deg, rgba(4,12,9,.99) 0%, rgba(4,12,9,.96) 62%, rgba(4,12,9,0) 100%)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 26,
              padding: "0 96px 26px",
              opacity: badge,
              transform: `translateY(${(1 - badge) * 10}px)`,
            }}
          >
            <span style={{ fontFamily: theme.mono, fontSize: 22, letterSpacing: ".34em", color: theme.lime }}>STEP 00 · GATE</span>
            <span style={{ fontFamily: theme.serif, fontSize: 46, color: theme.paper }}>先落在入口终端</span>
            <span style={{ fontFamily: theme.sans, fontSize: 24, color: "rgba(231,239,233,.6)" }}>浏览器打开服务地址，看到的第一屏</span>
          </div>

          <div style={{ display: "flex", gap: 30, padding: "0 96px", alignItems: "flex-start" }}>
            {gateBullets.map((item, index) => {
              const progress = interpolate(frame, [40 + index * 34, 70 + index * 34], [0, 1], ease);
              return (
                <div
                  key={item}
                  style={{
                    flex: 1,
                    display: "flex",
                    gap: 14,
                    opacity: progress,
                    transform: `translateY(${(1 - progress) * 16}px)`,
                  }}
                >
                  <span
                    style={{
                      marginTop: 11,
                      flex: "0 0 auto",
                      width: 11,
                      height: 11,
                      borderRadius: 3,
                      background: theme.lime,
                      transform: "rotate(45deg)",
                      boxShadow: "0 0 14px rgba(216,240,107,.45)",
                    }}
                  />
                  <span style={{ fontFamily: theme.sans, fontSize: 26, lineHeight: 1.5, color: theme.cream }}>{item}</span>
                </div>
              );
            })}
          </div>

          <div style={{ marginTop: 40, display: "flex", justifyContent: "center" }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: "18px 30px",
                borderRadius: 14,
                background: "rgba(8,22,16,.94)",
                border: `1px solid rgba(216,240,107,${0.26 + enter * 0.4})`,
                fontFamily: theme.mono,
                fontSize: 30,
                color: theme.cream,
              }}
            >
              <span style={{ color: theme.lime, fontWeight: 700 }}>λ</span>
              <span style={{ color: "rgba(231,239,233,.4)" }}>::</span>
              <span style={{ color: "#b3a7e0" }}>~</span>
              <span style={{ color: theme.teal }}>&gt;&gt;</span>
              <span>{TYPED.slice(0, typedCount)}</span>
              <span style={{ width: 15, height: 30, background: theme.lime, opacity: Math.floor(frame / 15) % 2 === 0 ? 1 : 0.25 }} />
              {enter > 0 ? <span style={{ marginLeft: 14, fontSize: 23, color: theme.teal, opacity: enter }}>↵ 进入制作台</span> : null}
            </div>
          </div>
        </div>

        <Watermark />
      </Backdrop>
    </AbsoluteFill>
  );
};
