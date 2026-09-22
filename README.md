# 口述史录音开放

管理历史录音数字化谱系、片段许可、转写修订与稳定引用。

## 分层模型

```
载体 Carrier
└─ 数字化批次 DigitizationBatch        同一载体可重复数字化，批次各自独立
   └─ 音频文件 AudioFile               母版 master / 衍生 derivative，只增不改
      └─ 音频片段 AudioSegment         切分范围与来源文件绑定，时间码以片段起点为 0
         ├─ 说话人 Speaker             真实姓名支持延迟解密（declassify_at）
         ├─ 转写版本 TranscriptionVersion → 转写行 TranscriptionLine
         ├─ 许可版本 LicenseVersion    公开收听 / 馆内收听 / 引用 / 下载
         ├─ 利用记录 AccessRecord      准许与拒绝都留痕，只增不改
         ├─ 纠错 Correction            待核查 → 已采纳 / 已驳回
         ├─ 考证决定 ResearchDecision / 年代修订 DatingRevision
         ├─ 发布批次 ReleaseBatch      首批录音分次上线
         ├─ 稳定引用 Citation          锁定版本与时间码，旧引用永远可解析
         └─ 关联文献 Document
```

## 关键规则

- **母版不覆盖**：仓库没有更新/删除音频文件的方法。降噪、格式迁移都生成新衍生品，
  登记来源文件与来源当时的 SHA-256；`GET /api/files/{id}/provenance` 逐级回溯到母版并核验。
- **许可版本化、即时生效**：每次授权或权利人补充限制都产生新 `LicenseVersion`，
  访问评估只采用请求时刻已生效的最新版本；既有 `AccessRecord` 固定指向当时的版本，
  因此收紧立即约束新访问，此前的合法利用仍可佐证。
- **公众只见到获准部分**：公开收听须同时满足“当前许可授权 + 片段已在已上线批次中”；
  说话人真实姓名在解密日之前对公众遮蔽，馆员视图不受影响。
- **纠错先核查**：听众纠错进入“待核查”；馆员采纳时复制当前版本全部行并套用修改，
  发布以纠错（或考证决定）为依据的新转写版本，旧版本保留。
- **旧时间码引用可解析**：引用锁定转写版本与区间；解析永远返回原版本行，
  同时附当前版本中时间重叠的对应行。
- **全链路溯源**：`GET /api/lines/{id}/trace` 从任一转写句追到声段、
  处理历史、数字化批次、载体、许可版本、考证决定、纠错去向、发布与关联文献。

## 运行

```bash
python3 service.py --check          # 基础配置与快照往返检查
python3 service.py --port 8000      # 内存模式
python3 service.py --data data.json # 启动载入快照，每次写操作后保存
npm test                            # 运行全部领域与服务契约测试（22 项）
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务身份 |
| GET | `/api/catalog/public` | 公众目录（已发布 + 许可公开，姓名按解密日遮蔽） |
| POST | `/api/carriers` | 登记载体 |
| POST | `/api/carriers/{id}/digitize` | 数字化，生成批次与母版（音频以 base64 提交） |
| POST | `/api/files/{id}/derive` | 降噪 `denoise` / 格式迁移 `migrate` 衍生品 |
| GET | `/api/files/{id}/provenance` | 来源链核验 |
| POST | `/api/speakers` / `/api/speakers/{id}/release-name` | 说话人与延迟解密 |
| POST | `/api/files/{id}/segments` | 切分片段 |
| POST | `/api/segments/{id}/seal` | 封存 |
| POST | `/api/documents` | 关联文献 |
| POST | `/api/segments/{id}/licenses` | 登记许可版本 |
| GET | `/api/segments/{id}/licenses` | 许可版本史 |
| POST | `/api/segments/{id}/access` | 访问请求（评估并留利用记录，拒绝同样返回 201 记录） |
| GET | `/api/access-records?segment_id=` | 利用记录 |
| POST | `/api/releases` | 发布批次 |
| POST | `/api/segments/{id}/transcription` | 初版转写 |
| POST | `/api/segments/{id}/corrections` | 听众纠错（line_id 或时间码） |
| POST | `/api/corrections/{id}/review` | 核查；`adopt:true` 即发布带依据新版本 |
| GET | `/api/corrections` | 纠错列表 |
| POST | `/api/segments/{id}/decisions` | 考证决定（年代/人名/内容） |
| POST | `/api/segments/{id}/datings` | 年代修订（历史保留） |
| POST | `/api/segments/{id}/retranscribe` | 依据考证决定重订转写 |
| POST | `/api/segments/{id}/citations` | 建立稳定引用 |
| GET | `/api/citations/{id}` | 解析引用（旧版本行 + 当前版本对应行） |
| GET | `/api/lines/{id}/trace` | 馆员全链路溯源 |

业务规则冲突返回 400，标识不存在返回 404，响应均为 UTF-8 JSON。
领域操作也可直接使用 `oralhistory.Engine`（见 `test_domain.py` 中的完整场景）。

`fixtures/domain.json` 保存领域词表（角色、状态、许可行为、纠错状态、考证类型等），
仅用于统一称谓；业务记录全部由接口或引擎产生。
