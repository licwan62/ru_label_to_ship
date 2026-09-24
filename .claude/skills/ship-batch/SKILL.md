---
name: ship-batch
description: Run a new Ozon shipment batch through the full pipeline — dedupe against the previous batch, audit, build, zip, and publish to public/. Use when the user asks to process/run/ship a new input/<批次> directory, or mentions dedupe+audit+build+发布 as one flow.
---

# ship-batch

跑通一个新批次的完整流水线：去重（可选）→ audit → build → zip → 发布到 `public/`。
把机械性的路径拼接、参数发现自动化；**凡是需要业务判断或数据确认的地方，必须停下来问用户，绝不自动猜测**。

## 0. 输入约定

- 新批次目录：`input/<批次>/`（扁平目录，文件名含关键字自动识别）。
- 若新批次目录下还有一个子目录（例如 `包含<上一批次>`），装着"新旧合并"的原始标签 PDF + 发货单 PDF/CSV（供去重用），**不要**把这个子目录当成 `--input-dir` 直接跑 audit——`discover_flat` 不递归，会被忽略，但去重步骤需要显式用到它。
- 上一批次目录：默认取 `input/` 下按名称排序、批次号小于当前批次的最近一个目录；不确定就问用户。

## 1. 去重（仅当存在"合并了旧批次"的原始文件时才需要）

```
python -m src.cli dedupe \
  --old-labels   input/<上批次>/<上批次标签PDF> \
  --old-shipment input/<上批次>/<上批次发货单PDF或CSV> \
  --new-labels   input/<新批次>/<合并子目录>/<新标签PDF> \
  --new-shipment input/<新批次>/<合并子目录>/<新发货单PDF或CSV> \
  --output artifacts/<新批次>/去重
```

检查 `artifacts/<新批次>/去重/去重报告.json`：
- `新增标签中解析异常页（号码格式不标准，需人工核对原PDF）` 非空 → **停下**，把具体页码报给用户，可能是号码格式超出现有正则假设（历史上出过尾段两位数字 `NUMBER_RE` 只认单位数的 bug，遇到新的解析异常页要重新怀疑正则，而不是直接人工略过）。
- `新增发货单中疑似列错位的行（...）` 非空 → **停下**，这是发货单 PDF 转表格时两行拼在一起的信号，需要人工核对原 PDF 后补录，不能自动丢弃了事。
- `新增标签有号码但发货单无对应记录` / `新增发货单有号码但标签无对应页` 非空 → **停下**，号码集合对不上，报给用户。
- 全部为空才把 `去重/新增标签.pdf`、`去重/新增发货单.csv` 复制进 `input/<新批次>/`，覆盖同名文件。

如果新批次目录本身就是干净的新增数据（没有合并旧批次），跳过这一步，直接进入 audit。

## 2. 确定 audit/build 的三份输入

`discover_flat` 的限制：只要 `--labels`/`--shipment`/`--products` 里有任何一个没显式传，就会对**全部三个**都走目录内关键字发现；只要其中一个在目录里找不到候选就整体报错。**结论：只要三者中有一个需要显式指定，就把三个都显式传上**，不要只传缺的那个。

- `--labels`、`--shipment`：通常就是 `input/<新批次>/` 里的标签 PDF 和发货单 CSV/PDF（去重后应该已经在这里了）。
- `--products`：优先看新批次目录里有没有信息表（文件名含"信息表"/"上架"/"汇总"）。
  - **没有 → 停下问用户**是否复用最近一个含信息表的旧批次（例如 `input/<上批次>/`）。这是业务数据决策，不能自己替用户决定，即使旧表看起来是"最新的"。
- fuzzy_matches：检查 `artifacts/<新批次>/fuzzy_matches.json` 是否存在；不存在就从 `config/fuzzy_matches.json` 复制一份过去（这是已建立的项目惯例，见其他批次的 `artifacts/*/fuzzy_matches.json`），避免已知别名（如 `LADA-2121-LINK-S`）被误判为待确认。

## 3. Audit

```
python -m src.cli audit --input-dir input/<新批次> \
  --labels <上面确定的标签PDF> --shipment <上面确定的发货单> --products <上面确定的信息表>
```

- 退出码 0（全部通过）→ 进入第 4 步。
- 退出码 2（有待确认项）→ **停下**。打开 `artifacts/<新批次>/审计/<时间戳>/待确认/待确认清单.csv`，逐条报给用户，必要时结合 `完整匹配表.csv` 和原始标签/发货单文件定位到具体页码/行号（不要只报错误类型，要报到人能直接去核对原件的程度）。绝不替用户填 `manual_overrides.csv` 的尺码/材质/确认人等字段。
- 退出码 1 → 说明文件/参数/生成失败，照错误信息修，通常是路径或格式问题，不是业务判断，可以自己排查修复。

## 4. Build（仅在 audit 全部通过后）

```
python -m src.cli build --input-dir input/<新批次> \
  --labels <同上> --shipment <同上> --products <同上>
```

成功后产物在 `artifacts/<新批次>/output/发货包/`。

## 5. Zip 并发布到 public/

命名惯例（看 `public/` 下已有文件）：`<批次>-发货包.zip`（如 `0922-发货包.zip`、`0923-发货包.zip`）。压缩包内容是 `发货包/` 目录下的文件**直接铺平**，不要套一层 `发货包/` 外壳目录：

```python
import zipfile, os
src = "artifacts/<新批次>/output/发货包"
out = "public/<新批次>-发货包.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(src):
        for f in files:
            full = os.path.join(root, f)
            z.write(full, os.path.relpath(full, src))
```

## 6. 收尾汇报

一句话总结：审计通过条数、build 生成的分组 PDF 页数、zip 路径。如果中途在第 1/2/3 步停下来问过用户，要清楚说明卡在哪、需要用户提供什么，不要笼统说"遇到问题"。

## 红线（不可自动化的部分）

- 不替用户确认发货尺码、材质、是否跳过、货号归属——这些字段必须来自人工确认。
- 不静默丢弃解析异常/疑似拼接的行——必须report给用户核对原始 PDF。
- 不在没有确认的情况下复用别的批次的信息表——每次都要问一次（信息表可能已更新）。
- 遇到号码格式解析失败且不是已知的历史模式时，先怀疑正则/解析逻辑是否覆盖不全（参考 `src/extract_labels.py` 的 `NUMBER_RE`、`src/load_sources.py` 的 `SHIPMENT_NUMBER_RE`、`src/dedupe_batch.py` 的 `NUMBER_RE`），而不是想当然地当成脏数据人工跳过。
