import React from "react";
import { Composition } from "remotion";
import { Demo, TOTAL_FRAMES } from "./Demo";
import { FPS, HEIGHT, WIDTH } from "./theme";

export const RemotionRoot: React.FC = () => (
  <Composition id="SceneFlowDemo" component={Demo} durationInFrames={TOTAL_FRAMES} fps={FPS} width={WIDTH} height={HEIGHT} />
);
