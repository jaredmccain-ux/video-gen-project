import unittest

from short_drama.drama_writer import complete_story_document, parse_json_object, story_document_from_outline


class DramaWriterTests(unittest.TestCase):
    def test_parse_json_object_strips_markdown_fence(self):
        payload = parse_json_object("```json\n{\"title\": \"雨夜\"}\n```")
        self.assertEqual(payload["title"], "雨夜")

    def test_parse_json_object_recovers_embedded_object(self):
        payload = parse_json_object("好的，这是结果：{\"title\": \"旧桥\"} 完")
        self.assertEqual(payload["title"], "旧桥")

    def test_outline_fallback_never_returns_empty_story(self):
        outline = """# 短剧标题
《失约的日落》

# 一句话梗概
女白领误以为男友爽约。

# 主要角色
- **林晚**：28岁，短发。
- **陈屿**：30岁，男友。

# 故事核心因果链
海边等待，商场求婚。

## 场次01 0-15秒
- 地点：海边
## 场次02 15-30秒
- 地点：商场
"""
        document = story_document_from_outline(outline, "正式剧本正文", ["IMG01"])
        self.assertEqual(document["title"], "失约的日落")
        self.assertIn("误以为", document["logline"])
        self.assertEqual([item["name"] for item in document["characters"]], ["林晚", "陈屿"])
        self.assertGreaterEqual(len(document["beats"]), 2)
        self.assertEqual(document["screenplay"], "正式剧本正文")

    def test_complete_story_fills_missing_logline_from_previous(self):
        document = complete_story_document(
            {"title": "最后一班渡船", "beats": [{"beat_id": "B01", "duration_s": 120, "summary": "开场"}]},
            previous={"logline": "姐姐追到海湾找回救命药。", "full_story": "完整剧情"},
            screenplay="剧本",
        )
        self.assertEqual(document["logline"], "姐姐追到海湾找回救命药。")
        self.assertEqual(document["full_story"], "完整剧情")
        self.assertEqual(document["title"], "最后一班渡船")


if __name__ == "__main__":
    unittest.main()
