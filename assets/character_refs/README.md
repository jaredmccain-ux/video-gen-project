# 角色参考包

故事规划会优先用 `story.json` 中每个角色的 `reference_image_ids`，把三张锚点图作为自动 Ref2VA 参考。为了获得更稳定的脸和服装，建议再准备每名主要角色的：

- 正面近景
- 3/4 侧脸
- 全身固定服装

图片可放在本目录任意子目录，并在项目配置中按角色 ID 指定：

```yaml
identity_consistency:
  character_references:
    C01:
      - assets/character_refs/C01/front.png
      - assets/character_refs/C01/three_quarter.png
      - assets/character_refs/C01/full_body.png
```

手工参考图优先，故事绑定的锚点图会作为补充。`prepare-consistency` 会把这些图登记到 run 的 `03_shots/character_portraits.json`；若开启 `image_generator`，还会尝试补齐缺失的正/侧/背静帧。智能选图阶段再按镜头挑选最多 8 张交给 Ref2VA。

Ref2VA 最多接收 9 张图；流水线按“选中参考 → 角色参考 → 场景锚点 → 上一镜末帧”的顺序去重并截断。

角色 ID 来自当前 run 的 `02_story/story.json`。如果更换故事，必须重新核对 ID 与图片映射，不能仅凭旧的 C01/C02 编号复用。
