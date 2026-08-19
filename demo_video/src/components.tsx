import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { theme } from "./theme";
import { STAGE_NAMES } from "./script";

export const GLYPHS: Record<string, string> = {
  A: ".###.|#...#|#####|#...#|#...#",
  B: "####.|#...#|####.|#...#|####.",
  C: ".####|#....|#....|#....|.####",
  D: "####.|#...#|#...#|#...#|####.",
  E: "#####|#....|####.|#....|#####",
  F: "#####|#....|####.|#....|#....",
  G: ".####|#....|#..##|#...#|.####",
  H: "#...#|#...#|#####|#...#|#...#",
  I: "#####|..#..|..#..|..#..|#####",
  J: "#####|...#.|...#.|#..#.|.##..",
  K: "#...#|#..#.|###..|#..#.|#...#",
  L: "#....|#....|#....|#....|#####",
  M: "#...#|##.##|#.#.#|#...#|#...#",
  N: "#...#|##..#|#.#.#|#..##|#...#",
  O: ".###.|#...#|#...#|#...#|.###.",
  P: "####.|#...#|####.|#....|#....",
  Q: ".###.|#...#|#.#.#|#..#.|.##.#",
  R: "####.|#...#|####.|#..#.|#...#",
  S: ".####|#....|.###.|....#|####.",
  T: "#####|..#..|..#..|..#..|..#..",
  U: "#...#|#...#|#...#|#...#|.###.",
  V: "#...#|#...#|#...#|.#.#.|..#..",
  W: "#...#|#...#|#.#.#|##.##|#...#",
  X: "#...#|.#.#.|..#..|.#.#.|#...#",
  Y: "#...#|.#.#.|..#..|..#..|..#..",
  Z: "#####|...#.|..#..|.#...|#####",
  " ": ".....|.....|.....|.....|.....",
};

export const Backdrop: React.FC<{ children?: React.ReactNode }> = ({ children }) => (
  <AbsoluteFill style={{ backgroundColor: theme.bg }}>
    <AbsoluteFill
      style={{
        background: `radial-gradient(circle at 22% 18%, rgba(216,240,107,.07), transparent 46%), radial-gradient(circle at 86% 88%, rgba(35,110,85,.18), transparent 52%)`,
      }}
    />
    <AbsoluteFill
      style={{
        opacity: 0.5,
        background: `repeating-linear-gradient(to right, rgba(216,240,107,.035) 0 1px, transparent 1px 120px), repeating-linear-gradient(to bottom, rgba(216,240,107,.035) 0 1px, transparent 1px 120px)`,
      }}
    />
    {children}
    <AbsoluteFill style={{ background: "radial-gradient(ellipse at center, transparent 55%, rgba(2,8,6,.7) 100%)" }} />
  </AbsoluteFill>
);

// Draws a word with the 5x5 block font, each pixel popping in with a stagger.
export const PixelWord: React.FC<{
  text: string;
  pixel: number;
  color?: string;
  glow?: boolean;
  appear?: { from: number; span: number };
}> = ({ text, pixel, color = theme.lime, glow = true, appear }) => {
  const frame = useCurrentFrame();
  const letters = text.toUpperCase().split("");
  const columnsTotal = letters.length * 6 - 1;

  return (
    <div style={{ display: "flex", gap: pixel }}>
      {letters.map((char, letterIndex) => {
        const glyph = (GLYPHS[char] ?? GLYPHS[" "]).split("|");
        return (
          <div key={`${char}-${letterIndex}`} style={{ display: "grid", gridTemplateRows: `repeat(5, ${pixel}px)`, gap: 0 }}>
            {glyph.map((row, rowIndex) => (
              <div key={rowIndex} style={{ display: "grid", gridTemplateColumns: `repeat(5, ${pixel}px)` }}>
                {row.split("").map((cell, colIndex) => {
                  if (cell !== "#") return <div key={colIndex} />;
                  const order = (letterIndex * 6 + colIndex) / columnsTotal;
                  const start = appear ? appear.from + order * appear.span : 0;
                  const local = appear ? interpolate(frame, [start, start + 10], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }) : 1;
                  return (
                    <div
                      key={colIndex}
                      style={{
                        width: pixel,
                        height: pixel,
                        backgroundColor: color,
                        opacity: local,
                        transform: `scale(${0.4 + local * 0.6})`,
                        boxShadow: glow ? `0 0 ${pixel * 1.6}px rgba(216,240,107,${0.42 * local})` : undefined,
                      }}
                    />
                  );
                })}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
};

export const ProgressRail: React.FC<{ active: number }> = ({ active }) => (
  <div style={{ position: "absolute", top: 46, left: 96, right: 96, display: "flex", alignItems: "center", gap: 10 }}>
    {STAGE_NAMES.map((name, index) => {
      const done = index < active;
      const current = index === active;
      return (
        <div key={name} style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8 }}>
          <div
            style={{
              height: 4,
              borderRadius: 99,
              background: current ? theme.lime : done ? "rgba(216,240,107,.42)" : "rgba(231,239,233,.14)",
              boxShadow: current ? `0 0 18px rgba(216,240,107,.5)` : undefined,
            }}
          />
          <div
            style={{
              fontFamily: theme.sans,
              fontSize: 15,
              letterSpacing: ".08em",
              color: current ? theme.lime : done ? "rgba(231,239,233,.6)" : "rgba(231,239,233,.3)",
              fontWeight: current ? 700 : 500,
            }}
          >
            {String(index + 1).padStart(2, "0")} {name}
          </div>
        </div>
      );
    })}
  </div>
);

export const Watermark: React.FC = () => (
  <div
    style={{
      position: "absolute",
      right: 62,
      bottom: 46,
      display: "flex",
      alignItems: "center",
      gap: 12,
      fontFamily: theme.mono,
      fontSize: 17,
      letterSpacing: ".22em",
      color: "rgba(231,239,233,.34)",
    }}
  >
    <span style={{ width: 9, height: 9, borderRadius: 99, background: theme.lime, opacity: 0.75 }} />
    SCENEFLOW
  </div>
);

export const Bullets: React.FC<{ items: string[]; from: number; stagger: number; dim: number }> = ({ items, from, stagger, dim }) => {
  const frame = useCurrentFrame();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      {items.map((item, index) => {
        const start = from + index * stagger;
        const progress = interpolate(frame, [start, start + 18], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
        return (
          <div
            key={item}
            style={{
              display: "flex",
              gap: 16,
              opacity: progress * (1 - dim),
              transform: `translateX(${(1 - progress) * 26}px)`,
            }}
          >
            <span
              style={{
                marginTop: 13,
                flex: "0 0 auto",
                width: 10,
                height: 10,
                borderRadius: 3,
                background: theme.lime,
                transform: "rotate(45deg)",
                boxShadow: "0 0 14px rgba(216,240,107,.45)",
              }}
            />
            <span style={{ fontFamily: theme.sans, fontSize: 27, lineHeight: 1.55, color: theme.cream }}>{item}</span>
          </div>
        );
      })}
    </div>
  );
};

export const fadeInOut = (frame: number, duration: number, pad = 14) =>
  interpolate(frame, [0, pad, duration - pad, duration], [0, 1, 1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
