import React from "react";
import { AbsoluteFill, Audio, Easing, interpolate, OffthreadVideo, staticFile, useCurrentFrame } from "remotion";
import { theme } from "../theme";
import { Backdrop, PixelWord, Watermark, fadeInOut } from "../components";

const ease = { easing: Easing.bezier(0.3, 0, 0.2, 1), extrapolateLeft: "clamp" as const, extrapolateRight: "clamp" as const };

export const Mosaic: React.FC<{ duration: number }> = ({ duration }) => {
  const frame = useCurrentFrame();
  const count = Math.round(interpolate(frame, [16, 92], [0, 16], ease));
  const caption = interpolate(frame, [10, 38], [0, 1], ease);
  const foot = interpolate(frame, [96, 124], [0, 1], ease);

  return (
    <AbsoluteFill style={{ opacity: fadeInOut(frame, duration) }}>
      <Backdrop>
        <AbsoluteFill style={{ filter: "brightness(.62) saturate(.95)" }}>
          <OffthreadVideo src={staticFile("film/mosaic.mp4")} muted style={{ width: "100%", height: "100%", objectFit: "cover" }} />
        </AbsoluteFill>
        <AbsoluteFill style={{ background: "linear-gradient(180deg, rgba(5,14,10,.9) 0%, rgba(5,14,10,.25) 38%, rgba(5,14,10,.92) 100%)" }} />

        <div style={{ position: "absolute", left: 0, right: 0, top: 148, textAlign: "center", opacity: caption }}>
          <div style={{ fontFamily: theme.mono, fontSize: 24, letterSpacing: ".4em", color: theme.lime, marginBottom: 22 }}>COMFYUI QUEUE</div>
          <div style={{ fontFamily: theme.serif, fontSize: 74, color: theme.paper }}>
            <span style={{ color: theme.lime, fontFamily: theme.mono, fontWeight: 700 }}>{count}</span>
            <span style={{ fontFamily: theme.mono, color: "rgba(231,239,233,.5)" }}> / 16</span> 个镜头生成完成
          </div>
        </div>

        <div style={{ position: "absolute", left: 0, right: 0, bottom: 132, textAlign: "center", opacity: foot }}>
          <div style={{ fontFamily: theme.sans, fontSize: 32, color: theme.cream, marginBottom: 14 }}>
            每一镜都是人工确认输入后才提交的，不合意可以单镜重生成
          </div>
          <div style={{ fontFamily: theme.mono, fontSize: 23, letterSpacing: ".18em", color: "rgba(111,211,165,.9)" }}>
            16 clips · 8s each · 1344 × 768
          </div>
        </div>

        <Watermark />
      </Backdrop>
    </AbsoluteFill>
  );
};

export const Showcase: React.FC<{ duration: number }> = ({ duration }) => {
  const frame = useCurrentFrame();
  const card = interpolate(frame, [12, 40, 150, 182], [0, 1, 1, 0], ease);
  const outro = interpolate(frame, [duration - 90, duration - 60], [0, 1], ease);

  return (
    <AbsoluteFill style={{ opacity: fadeInOut(frame, duration, 20), backgroundColor: "#04100b" }}>
      <AbsoluteFill>
        <OffthreadVideo src={staticFile("film/highlight.mp4")} muted style={{ width: "100%", height: "100%", objectFit: "cover" }} />
      </AbsoluteFill>
      <Audio src={staticFile("film/highlight.m4a")} volume={(f) => interpolate(f, [0, 20, duration - 40, duration - 10], [0, 1, 1, 0], ease)} />

      <div
        style={{
          position: "absolute",
          left: 84,
          top: 84,
          padding: "26px 34px",
          borderRadius: 18,
          background: "rgba(6,17,12,.82)",
          border: "1px solid rgba(216,240,107,.3)",
          opacity: card,
          transform: `translateY(${(1 - card) * -14}px)`,
        }}
      >
        <div style={{ fontFamily: theme.mono, fontSize: 21, letterSpacing: ".34em", color: theme.lime, marginBottom: 14 }}>FINAL CUT</div>
        <div style={{ fontFamily: theme.serif, fontSize: 52, color: theme.paper, marginBottom: 12 }}>《下一场，还一起》</div>
        <div style={{ fontFamily: theme.sans, fontSize: 25, color: "rgba(231,239,233,.72)" }}>16 镜 · 128.1 秒 · 硬字幕已按人声对齐并烧录</div>
      </div>

      <AbsoluteFill
        style={{
          justifyContent: "flex-end",
          alignItems: "center",
          paddingBottom: 96,
          background: `linear-gradient(180deg, transparent 60%, rgba(4,14,9,${0.9 * outro}) 100%)`,
          opacity: outro,
        }}
      >
        <div style={{ fontFamily: theme.sans, fontSize: 30, color: theme.cream }}>以上片段由同一条流水线产出，未做二次剪辑</div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

export const Outro: React.FC<{ duration: number }> = ({ duration }) => {
  const frame = useCurrentFrame();
  const line = interpolate(frame, [24, 52], [0, 1], ease);
  const urls = interpolate(frame, [58, 86], [0, 1], ease);

  return (
    <AbsoluteFill style={{ opacity: fadeInOut(frame, duration, 20) }}>
      <Backdrop>
        <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", gap: 46 }}>
          <div style={{ transform: "scale(.72)" }}>
            <PixelWord text="SCENEFLOW" pixel={13} appear={{ from: 0, span: 30 }} />
          </div>
          <div
            style={{
              fontFamily: theme.serif,
              fontSize: 64,
              color: theme.paper,
              opacity: line,
              transform: `translateY(${(1 - line) * 14}px)`,
            }}
          >
            机器执行，镜头由人决定
          </div>
          <div style={{ display: "flex", gap: 34, opacity: urls, fontFamily: theme.mono, fontSize: 25 }}>
            <span style={{ padding: "14px 26px", borderRadius: 12, border: "1px solid rgba(216,240,107,.28)", color: theme.lime }}>入口 &nbsp;/</span>
            <span style={{ padding: "14px 26px", borderRadius: 12, border: "1px solid rgba(111,211,165,.28)", color: theme.teal }}>工作台 &nbsp;/studio</span>
          </div>
        </AbsoluteFill>
      </Backdrop>
    </AbsoluteFill>
  );
};