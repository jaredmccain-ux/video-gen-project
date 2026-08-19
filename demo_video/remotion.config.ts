import { Config } from "@remotion/cli/config";

Config.setVideoImageFormat("jpeg");
Config.setCodec("h264");
Config.setCrf(18);
Config.setOverwriteOutput(true);
// No GPU in this box, so keep Chromium on the software renderer.
Config.setChromiumOpenGlRenderer("swangle");
Config.setDelayRenderTimeoutInMilliseconds(120000);
