import unittest

from short_drama.h3_jobs import (
    build_h3_workflow,
    frames_for_duration,
    sampled_frames,
    shot_clip_index,
)


class H3MultimodalWorkflowTests(unittest.TestCase):
    def test_reference_video_and_audio_nodes_are_connected(self):
        workflow = build_h3_workflow(
            prompt="Use <Video 1> and <Audio 1> as references",
            width=864,
            height=480,
            length=124,
            seed=1,
            filename_prefix="test/h3",
            generation_mode="ref2va",
            ref_video_names=["reference.mp4"],
            ref_audio_names=["reference.wav"],
        )
        self.assertEqual(workflow["210"]["class_type"], "LoadVideo")
        self.assertEqual(workflow["211"]["class_type"], "GetVideoComponents")
        self.assertEqual(workflow["310"]["class_type"], "LoadAudio")
        self.assertEqual(workflow["10"]["inputs"]["ref_videos.ref_video_0"], ["211", 0])
        self.assertEqual(workflow["10"]["inputs"]["ref_audios.ref_audio_0"], ["310", 0])
        self.assertNotIn("70", workflow)
        self.assertNotIn("71", workflow)

    def test_motion_context_is_wired_for_later_clips(self):
        workflow = build_h3_workflow(
            prompt="continue the previous motion",
            width=864,
            height=480,
            length=158,
            seed=2,
            filename_prefix="test/h3",
            generation_mode="ref2va",
            ref_image_names=["anchor.png"],
            context_folder="short_drama/run-1",
            context_clip_index=1,
            save_clip_index=2,
            context_length="22",
            audio_context_length=24,
        )
        self.assertEqual(workflow["70"]["class_type"], "MiniMaxH3MotionContextLoadLatent")
        self.assertEqual(workflow["70"]["inputs"]["clip_index"], 1)
        self.assertEqual(workflow["70"]["inputs"]["latent_path"], "short_drama/run-1")
        self.assertEqual(workflow["71"]["class_type"], "MiniMaxH3MotionContext")
        self.assertEqual(workflow["71"]["inputs"]["context_latent"], ["70", 0])
        self.assertEqual(workflow["71"]["inputs"]["context_length"], "22")
        self.assertEqual(workflow["23"]["inputs"]["conditioning"], ["71", 0])
        self.assertEqual(workflow["72"]["class_type"], "MiniMaxH3MotionContextTrim")
        self.assertEqual(workflow["72"]["inputs"]["trim_frames"], ["71", 1])
        self.assertEqual(workflow["50"]["inputs"]["images"], ["72", 0])
        self.assertEqual(workflow["50"]["inputs"]["audio"], ["72", 1])
        self.assertEqual(workflow["73"]["class_type"], "MiniMaxH3MotionContextSaveLatent")
        self.assertEqual(workflow["73"]["inputs"]["clip_index"], 2)
        self.assertEqual(workflow["73"]["inputs"]["latent"], ["30", 0])

    def test_first_clip_saves_latent_without_loading_context(self):
        workflow = build_h3_workflow(
            prompt="cold start",
            width=864,
            height=480,
            length=158,
            seed=3,
            filename_prefix="test/h3",
            generation_mode="t2va",
            context_folder="short_drama/run-1",
            save_clip_index=1,
        )
        self.assertNotIn("70", workflow)
        self.assertNotIn("71", workflow)
        self.assertEqual(workflow["23"]["inputs"]["conditioning"], ["10", 0])
        self.assertEqual(workflow["50"]["inputs"]["images"], ["40", 0])
        self.assertEqual(workflow["73"]["inputs"]["clip_index"], 1)

    def test_frame_grid_snaps_up_like_the_reference_graph(self):
        self.assertEqual(frames_for_duration(6), 158)
        self.assertEqual(frames_for_duration(5), 124)
        self.assertEqual(frames_for_duration(4), 107)
        self.assertEqual(sampled_frames(6, context_frames=22), 175)
        self.assertEqual(shot_clip_index("S012"), 12)


if __name__ == "__main__":
    unittest.main()
