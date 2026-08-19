import React from "react";
import { AbsoluteFill, Sequence } from "remotion";
import { theme } from "./theme";
import { stages } from "./script";
import { Intro } from "./scenes/Intro";
import { Gate } from "./scenes/Gate";
import { Overview } from "./scenes/Overview";
import { Stage } from "./scenes/Stage";
import { Mosaic, Outro, Showcase } from "./scenes/Film";

const INTRO = 300;
const GATE = 330;
const OVERVIEW = 420;
const MOSAIC = 240;
const SHOWCASE = 600;
const OUTRO = 210;

type Block = { key: string; duration: number; render: () => React.ReactNode };

// The mosaic lands right after 人工编排 so "confirm, then the machine renders"
// reads as one thought.
export const buildTimeline = (): Block[] => {
  const blocks: Block[] = [
    { key: "intro", duration: INTRO, render: () => <Intro duration={INTRO} /> },
    { key: "gate", duration: GATE, render: () => <Gate duration={GATE} /> },
    { key: "overview", duration: OVERVIEW, render: () => <Overview duration={OVERVIEW} /> },
  ];

  stages.forEach((spec, index) => {
    blocks.push({ key: spec.id, duration: spec.durationInFrames, render: () => <Stage spec={spec} order={index} /> });
    if (spec.id === "orchestration") {
      blocks.push({ key: "mosaic", duration: MOSAIC, render: () => <Mosaic duration={MOSAIC} /> });
    }
  });

  blocks.push({ key: "showcase", duration: SHOWCASE, render: () => <Showcase duration={SHOWCASE} /> });
  blocks.push({ key: "outro", duration: OUTRO, render: () => <Outro duration={OUTRO} /> });
  return blocks;
};

export const TOTAL_FRAMES = buildTimeline().reduce((sum, block) => sum + block.duration, 0);

export const Demo: React.FC = () => {
  let cursor = 0;
  return (
    <AbsoluteFill style={{ backgroundColor: theme.bg }}>
      {buildTimeline().map((block) => {
        const from = cursor;
        cursor += block.duration;
        return (
          <Sequence key={block.key} from={from} durationInFrames={block.duration} name={block.key}>
            {block.render()}
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
