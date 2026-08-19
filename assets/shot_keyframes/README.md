# 可选的 FL2VA 末帧关键帧

把人工确认过的镜头末帧放在本目录，命名为：

```text
S001.last.png
S002.last.jpg
```

执行 `prepare-consistency`（或重新 `plan-shots` 后再跑一致性预处理）时：

- medium/large 变化镜头会优先使用这些手工末帧；
- 若开启 `image_generator`，也会自动生成候选关键帧并择优，同步到本目录与 run 内 `03_shots/keyframes/`；
- 只要该镜头同时拥有首帧来源（`prepared_first_frame`、场景锚点或紧邻上一镜末帧），就会路由为真正的 `first_last_frame`。

```text
first_frame + Sxxx.last.png -> MiniMaxH3ImageToVideo (FL2VA)
```

关键帧应与该镜头计划的末帧构图、人物、服装、道具和方向一致。没有对应文件且未生成关键帧时，流水线不会伪造末帧，而是使用 Ref2VA 周期重锚或普通首帧续接。
