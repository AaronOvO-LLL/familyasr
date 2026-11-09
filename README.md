# familyasr（家语字幕屏）

为听力不佳的家人提供实时字幕，核心语言为普通话、昆明话、上海话及自然混用。
首版为 Web 应用（非 APK）：Windows 上运行 Python FastAPI 服务，Android Pad 亮屏前台收音并显示大字号字幕。

## 当前状态（2026-11-09）

| 阶段 | 状态 | 说明 |
|---|---|---|
| M0 可行性验证 | **未通过（验证未执行）** | 测试方案与最小验证工具已就绪；真实方言识别验证待凭据、家人参与 |
| M1 Windows 最小 Web 应用 | 代码交付，部分待验证 | 核心界面/采音/腾讯云链路适配/启停清理/访问控制/用量限制已实现并通过自动化测试与 mock 全链路运行验证；15 分钟真实转写待腾讯云凭据 |
| M2 Android Pad | **未通过（待验收）** | 局域网受信任 HTTPS 试用条件与说明已备（见下）；Pad 实机收音、阅读体验与三种语言效果待家庭实测 |
| M3 云端 | 未开始 | 本轮不部署公网 |

诚实口径：mock 模式与自动化测试不构成真实识别效果、时延、Pad 收音或费用证据；
家庭方言效果、Pad 收音与阅读体验保留待验收。

## 快速开始（Windows）

```text
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env        # 填 ACCESS_TOKEN；真实识别另填腾讯云凭据
run.cmd                       # 启动 http://127.0.0.1:7020/
```

浏览器打开 `http://127.0.0.1:7020/?token=<ACCESS_TOKEN>`。无腾讯云凭据时可先设
`ASR_PROVIDER=mock` 演练界面与流程（不产生云费用，不作为效果证据）。

## 局域网 Pad 试用（M2 条件准备）

见 [M2 局域网配置与试用说明](docs/implementation/M2-局域网配置与试用说明.md)：
mkcert 受信任 HTTPS + 防火墙仅专用网络 + Pad 安装根 CA。本轮不部署公网。

## 文档

- [当前需求基线：MVP 需求文档 V0.2](docs/requirements/MVP需求文档_V0.2.md)
- [Agent 工作与协作约定](AGENTS.md)
- [M1 任务书（本轮阶段授权）](docs/implementation/M1-任务书.md)
- [M1 接口约定（消息契约与状态语义）](docs/implementation/M1-接口约定.md)
- [M1 实施报告](docs/reports/M1-实施报告.md)
- [M0 测试方案与样本设计](docs/implementation/M0-测试方案.md) / [M0 结果记录表](docs/implementation/M0-结果记录表.md) / [M0 准备阶段报告](docs/reports/M0-准备阶段-报告.md)

## 实现进度

| 模块 | 状态 | 备注 |
|---|---|---|
| 核心界面（大字号/深色/回看/全屏/常亮） | 已实现 | 浏览器渲染已验证；阅读体验待 Pad 实测 |
| 浏览器采音（AudioWorklet→16k PCM） | 已实现 | 200ms/6400B 包；实机采音待验证 |
| 腾讯云实时 ASR V2 链路适配 | 已实现 | 签名/重试/错误分类经单测；真实链路调用待凭据 |
| Mock 提供方 | 已实现 | 临时→修订→定稿、空文本/重复/乱序/断线等边界脚本 |
| 启停与资源清理 | 已实现 | 3 秒尾句、心跳空闲清理、断开即清理 |
| 访问控制 | 已实现 | 家庭访问码 + Cookie + WS 同源校验 |
| 用量限制 | 已实现 | 服务端硬配额，持久化、北京时间自然日重置 |
| 局域网 HTTPS 配置与试用说明 | 已交付 | mkcert 方案，待家中实测 |
| 声纹识别 / 多说话人专项 / 公网部署 | 暂缓/不在范围 | 按需求与本轮授权不做 |

## 测试

```text
.venv\Scripts\python -m pytest tests/    # 56 项（含 M0 工具回归）
node tests/js/test_merge.js             # 前端字幕归并逻辑
```

凭据配置见 `.env.example`（真实密钥只放 `.env`/环境变量，不入库；日志只记元数据）。
需求文档 V0.2 对应的产品仍是 V0.1 / MVP，不包含多说话人专项或声纹身份识别的开发授权。
