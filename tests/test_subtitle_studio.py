import unittest

from short_drama.subtitle_studio import (
    align_cues_to_speech,
    ass_time,
    build_ass,
    cue_window,
    default_style,
    normalize_style,
    voiced_window,
    wrap_subtitle_text,
)


def cue(text, **extra):
    return {"shot_id": "S001", "speaker_id": "C01", "text": text, **extra}


def segment(start, end, text, *, energy=None, frame_s=0.02):
    return {"start": start, "end": end, "text": text, "energy": energy or [], "frame_s": frame_s}


class AssRenderingTests(unittest.TestCase):
    def test_style_defaults_scale_with_the_delivered_resolution(self):
        reference = default_style(768)
        small = default_style(480)
        self.assertEqual(reference["font_size"], 44)
        self.assertEqual(small["font_size"], 28)
        self.assertLess(small["margin_v"], reference["margin_v"])

    def test_style_values_are_clamped_to_renderable_ranges(self):
        style = normalize_style({"font_size": 900, "outline": -3, "alignment": 42, "max_lines": 9})
        self.assertEqual(style["font_size"], 160)
        self.assertEqual(style["outline"], 0.0)
        self.assertEqual(style["alignment"], 2)
        self.assertEqual(style["max_lines"], 3)

    def test_long_lines_break_at_punctuation_within_the_line_budget(self):
        wrapped = wrap_subtitle_text("电影又不会跑。以前让你上学，你怎么没这么积极？", max_chars=14, max_lines=2)
        self.assertEqual(wrapped, "电影又不会跑。以前让你上学，\\N你怎么没这么积极？")

    def test_short_lines_stay_on_one_line(self):
        self.assertEqual(wrap_subtitle_text("说好了。", max_chars=18, max_lines=2), "说好了。")

    def test_ass_uses_video_resolution_and_cinema_style_row(self):
        document = build_ass(
            [cue("说好了。", film_start_s=1.5, film_end_s=2.25)],
            style=default_style(768),
            width=1344,
            height=768,
            title="下一场，还一起",
        )
        self.assertIn("PlayResX: 1344", document)
        self.assertIn("Style: Default,Noto Sans CJK SC,44,&H00FFFFFF", document)
        # BorderStyle 1 = outline plus shadow; an opaque caption box would be 3.
        self.assertIn(",0.4,0,1,2.6,0.8,2,70,70,46,1", document)
        self.assertIn("Dialogue: 0,0:00:01.50,0:00:02.25,Default,C01,0,0,0,,{\\fad(80,60)}说好了。", document)

    def test_ass_skips_empty_and_inverted_cues(self):
        document = build_ass(
            [cue("", film_start_s=0, film_end_s=2), cue("反了", film_start_s=5, film_end_s=4)],
            style=default_style(768),
            width=1344,
            height=768,
        )
        self.assertNotIn("Dialogue:", document)

    def test_ass_time_uses_centiseconds(self):
        self.assertEqual(ass_time(3661.239), "1:01:01.24")

    def test_planned_cues_fall_back_to_the_shot_offset(self):
        self.assertEqual(cue_window({"planned_start_s": 12.0, "start_s": 0.8, "end_s": 5.5}), (12.8, 17.5))


class SpeechAlignmentTests(unittest.TestCase):
    def test_one_window_holding_several_lines_is_split_between_them(self):
        cues = [cue("快点"), cue("电影又不会跑")]
        result = align_cues_to_speech(cues, [segment(2.5, 10.0, "快点电影又不会跑")], video_duration_s=20)
        self.assertEqual(result["matched_cue_count"], 2)
        self.assertAlmostEqual(cues[0]["film_start_s"], 2.5, places=2)
        self.assertAlmostEqual(cues[1]["film_end_s"], 10.0, places=2)
        self.assertLess(cues[0]["film_end_s"], cues[1]["film_start_s"])

    def test_merged_windows_keep_their_silence_instead_of_spreading_across_it(self):
        cues = [cue("票呢"), cue("明明放这里了"), cue("最后一场哦")]
        segments = [segment(30.0, 34.0, "票呢明明放这里了"), segment(40.0, 43.0, "最后一场哦")]
        align_cues_to_speech(cues, segments, video_duration_s=60)
        self.assertLessEqual(cues[1]["film_end_s"], 34.01)
        self.assertGreaterEqual(cues[2]["film_start_s"], 40.0)

    def test_wording_always_comes_from_the_script_not_the_recognizer(self):
        cues = [cue("因为以前你不会明天就走。")]
        align_cues_to_speech(cues, [segment(5.0, 9.0, "李烨因为以前你不会明天就走")], video_duration_s=20)
        self.assertEqual(cues[0]["text"], "因为以前你不会明天就走。")
        self.assertEqual(cues[0]["timing_source"], "asr_sensevoice_vad")

    def test_unheard_lines_still_get_a_readable_window_after_the_last_match(self):
        cues = [cue("说好了。"), cue("这句没被识别到。")]
        result = align_cues_to_speech(cues, [segment(1.0, 3.0, "说好了")], video_duration_s=30)
        self.assertEqual(result["matched_cue_count"], 1)
        self.assertEqual(cues[1]["timing_source"], "asr_unmatched_interpolated")
        self.assertGreater(cues[1]["film_start_s"], cues[0]["film_end_s"])

    def test_cues_never_run_past_the_end_of_the_film(self):
        cues = [cue("说好了。")]
        align_cues_to_speech(cues, [segment(9.0, 30.0, "说好了")], video_duration_s=10)
        self.assertLessEqual(cues[0]["film_end_s"], 10.0)

    def test_leading_ambience_is_trimmed_off_the_speech_window(self):
        quiet = [50] * 50
        loud = [6000] * 100
        window = voiced_window(segment(0.0, 3.0, "台词", energy=quiet + loud))
        self.assertGreater(window[0], 0.8)

    def test_windows_without_an_energy_envelope_are_used_as_is(self):
        self.assertEqual(voiced_window(segment(1.0, 4.0, "台词")), (1.0, 4.0))


if __name__ == "__main__":
    unittest.main()
