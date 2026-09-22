# 口述史录音开放

管理历史录音的数字化谱系、片段级许可、转写修订与稳定引用。服务以
`domain.py`（纯领域层，无 HTTP 依赖）与 `service.py`（JSON API）两层构成。

## 模型分层

```
载体 Carrier
 └─ 数字化批次 DigitizationBatch（同一载体可重复数字化，母版各自独立保留）
      └─ 数字母版 AudioFile（不可变）
           └─ 处理事件 ProcessEvent：降噪 / 切分 / 格式迁移
                └─ 派生 AudioFile …（只追加，永不覆写母版）
                     └─ 片段 Clip
                          ├─ 说话人 Speaker（真实姓名可延迟解密）
                          ├─ 转写版本 TranscriptVersion → 转写行 TranscriptLine
                          ├─ 纠错 Correction（待核查 → 已采纳/已驳回）
                          ├─ 稳定引用 Citation（冻结版本号与时间码）
                          ├─ 关联文献 Document / 考证决定 ResearchDecision
                          └─ 捐赠协议 DonorAgreement + 补充限制 Restriction
```

## 核心规则

- **母版不可变**：每次降噪、切分、格式迁移都生成新派生文件与处理事件，
  可经 `provenance` 从任一派生文件回溯到母版，校验 checksum 验证来源。
- **片段级许可**：捐赠协议按片段授予 `public_listen` / `onsite_listen` /
  `cite` / `download` 四项权限；协议在签署时间之后生效。
- **补充限制即时生效**：权利人追加限制后，新访问立即被拒绝；此前发生的合法
  利用记录保留（`usage`），并标注当前是否仍允许，不做追溯删除。
- **分次上线**：片段只有进入已上线波次、且当前仍有公众收听权，才出现在
  公众目录；限制变化会即时把已上线片段撤出公众目录。
- **转写版本化**：已发布版本不可修改；听众纠错先进核查，馆员采纳后发布带
  依据的新版本，旧版转为“已撤回”但继续保留。
- **稳定引用**：引用在生成时冻结版本号、行号与时间码；再版后旧引用经版本
  行映射与时间码映射继续解析；行被合并/删除时如实标注并定位覆盖该声段的
  当前行，绝不静默指向错误内容。
- **延迟解密**：说话人真实姓名设 `reveal_after` 到期前，公众只见化名；馆员
  在内部视图中始终可见。
- **馆员溯源**：`/librarian/clips/{id}/trace` 一次返回片段 → 原始声段 →
  处理历史 → 许可版本 → 考证决定 → 纠错 → 利用记录的完整链路。

## 运行与测试

```bash
python3 service.py --check          # 基础配置检查
python3 service.py --port 8000     # 以内置演示数据启动
python3 service.py --data state.json  # 使用持久化文件（不存在则以演示数据启动）
npm test                           # 运行全部测试（34 个）
```

## HTTP 接口

写接口均为 `POST` + JSON 请求体；馆员接口需请求头
`X-Role: librarian`。领域校验错误返回 400，未开放/不存在返回 404。

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| GET | `/health` | 公开 | 服务身份 |
| GET | `/catalog` | 公开 | 公众目录（仅可收听片段） |
| GET | `/clips/{id}` | 公开/馆员 | 片段视图（未上线片段公众得到 404） |
| GET | `/transcripts/{cid}?version=n` | 公开/馆员 | 当前或指定版本转写 |
| GET | `/citations/resolve?token=…` | 公开 | 稳定引用解析 |
| POST | `/clips/{id}/access` | 公开 | 访问判定并留痕，body：`actor`,`perm` |
| POST | `/clips/{id}/corrections` | 公开 | 提交纠错（直接进入待核查） |
| POST | `/carriers` | 馆员 | 登记载体 |
| POST | `/carriers/{id}/digitize` | 馆员 | 登记数字化批次与母版（可重复） |
| POST | `/files/{id}/process` | 馆员 | 降噪/切分/格式迁移，生成派生文件 |
| GET | `/files/{id}/provenance` | 馆员 | 文件 → 母版谱系 |
| POST | `/speakers` | 馆员 | 登记说话人（支持延迟解密） |
| POST | `/clips` | 馆员 | 建立片段 |
| POST | `/documents` | 馆员 | 关联文献 |
| POST | `/clips/{id}/research` | 馆员 | 考证决定 |
| POST | `/agreements` | 馆员 | 捐赠协议（按片段授权） |
| POST | `/clips/{id}/restrictions` | 馆员 | 追加补充限制（即时生效） |
| POST | `/restrictions/{id}/lift` | 馆员 | 解除限制 |
| POST | `/waves` | 馆员 | 创建上线波次 |
| POST | `/waves/{id}/clips` | 馆员 | 波次分次追加片段 |
| POST | `/waves/{id}/withdraw` | 馆员 | 撤回波次 |
| POST | `/clips/{id}/transcripts` | 馆员 | 保存转写新版本 |
| POST | `/transcripts/{cid}/{ver}/publish` | 馆员 | 发布版本 |
| POST | `/corrections/{id}/review` | 馆员 | 核查纠错（采纳并可直接发布新版） |
| GET | `/corrections` | 馆员 | 纠错队列（可按状态过滤） |
| POST | `/clips/{id}/citations` | 馆员 | 铸造稳定引用 |
| GET | `/clips/{id}/permissions` | 公开/馆员 | 查看当前实际许可 |
| GET | `/clips/{id}/usage` | 馆员 | 利用记录 |
| GET | `/librarian/clips` | 馆员 | 全部片段（含未上线） |
| GET | `/librarian/clips/{id}/trace` | 馆员 | 全链路溯源 |

## 领域词汇

统一称谓见 `fixtures/domain.json`（角色、状态、许可项与关键规则）；
业务记录一律由正式接口产生，不直接写该文件。
