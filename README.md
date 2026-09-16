# 中国平安反转指标跟踪 · 云端版

跑在 GitHub Actions 上的定时跟踪任务。电脑关机也能执行，报告推送到企业微信群机器人。

- **触发时间**：每周一、三、五 北京时间 08:30（GitHub Actions 可能延迟 5–20 分钟）
- **推送内容**：Markdown 摘要（关键指标 + 触发判定 + 建议），报告 HTML 作为 Actions 产物留存
- **运行开销**：纯标准库，无第三方依赖，单次约 10 秒

---

## 一、先拿到企微群机器人 webhook（30 秒）

1. 在企业微信里**建一个群**。建议：拉一位同事进群建好后，再把对方移出——群会保留，机器人也还在。
2. 群聊右上角 **⋯** → **群机器人** → **添加机器人**
3. 名字随便起，比如「平安跟踪」，头像随意 → 创建
4. 复制 **Webhook 地址**，形如：
   ```
   https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   ```

> ⚠️ 这个地址等同于「往群里发消息」的权限，**不要提交到公开仓库、不要发给别人**。
> 本项目只从环境变量读取它，不会写进任何文件。

> 说明：群机器人只能发**文本 / Markdown**，不能发文件附件。因此报告以 Markdown 摘要形式直接发到群里（手机上能看全），
> 完整 HTML 报告作为 Actions 产物保留 90 天，需要时去 Actions 页面下载。

---

## 二、部署到 GitHub Actions

### 1. 建仓库并推送

```bash
cd pingan-tracker-cloud
git init
gh repo create pingan-tracker --private --source=. --push
# 或者手动在 GitHub 建仓库后：
# git remote add origin git@github.com:<你的用户名>/pingan-tracker.git
# git push -u origin main
```

### 2. 配置 Secret

仓库页面 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Name | Value |
|---|---|
| `WECOM_WEBHOOK_URL` | 第一步拿到的 webhook 地址（完整 URL） |

**先本地验证通道**（可选但推荐，能在 push 之前就知道 key 对不对）：

```bash
export TZ=Asia/Shanghai
export WECOM_WEBHOOK_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
python3 tracker.py --test-push
```

成功：群里收到「通道自检」消息，终端打印 `[通道测试] 成功 —— {"errcode":0,...}`
失败：终端打印具体 `errcode`，常见原因：

| errcode | 含义 | 处理 |
|---|---|---|
| `93000` | webhook URL 无效 | key 复制不全或机器人被删，重新复制 |
| `45009` | 消息发送频率超限 | 群机器人限 20 条/分钟，等一会重试 |
| 超时无响应 | 网络不可达 | 检查 Actions 所在网络能否访问 `qyapi.weixin.qq.com` |

### 3. 手动跑一次验证

仓库页面 → **Actions** → 左侧选「平安反转指标跟踪」→ 右上角 **Run workflow** → 选分支 → 绿色按钮运行。

约 20 秒后：
- 群里应该收到一条 Markdown 消息
- Actions 页面出现 `pingan-report-*` 产物，可下载 HTML 报告

### 4. 关于定时精度

`cron: '30 0 * * 1,3,5'` 是 **UTC 00:30 = 北京时间 08:30**。

GitHub Actions **不保证准点**，负载高时可能延迟 5–20 分钟，极端情况可能跳过。
如果某次没跑，去 Actions 页面点 **Run workflow** 手动补一次即可。

---

## 三、本地测试

```bash
export TZ=Asia/Shanghai
python3 tracker.py --dry-run     # 只生成报告，不推送
python3 tracker.py --test-push   # 只发一条测试消息，验证企微通道
```

报告输出到 `reports/YYYYMMDD.html`，摘要输出到 `reports/latest.md`。

要真实推送：

```bash
export WECOM_WEBHOOK_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
python3 tracker.py
```

---

## 四、判定规则

| 档位 | 触发条件（满足任一即升级） |
|---|---|
| 未到 | 10Y < 1.75% 且 P/EV < 0.65 |
| 观察 | 10Y ≥ 1.75%，或 P/EV ≥ 0.65，或保险板块连续 5 日跑赢沪深300 |
| 初步确认 | 10Y 连续两周 ≥ 1.85% **且** P/EV ≥ 0.70 |
| 确认 | 10Y ≥ 2.00%，或 P/EV ≥ 0.85 |

反向风险：10Y < 1.60% → 标记「恶化」，建议推迟介入。

阈值在 `config.json` 的 `thresholds` 段，随时可改。

---

## 五、需要人工维护的项

云端拿不到的数据集中在 `config.json` 的 `manual` 段：

| 字段 | 含义 | 维护频率 |
|---|---|---|
| `ev_total_yi` | 集团内含价值（亿元），用于算 P/EV | 每次中报/年报披露后 |
| `preset_rate_research` | 中保协普通型人身险预定利率研究值 | 每季度（1/4/7/10 月公布） |

当前 `ev_total_yi = 15666` 是**由 2026-09-15 时点市值 9713 亿 ÷ P/EV 0.62 反推**的估算值，
**请按中报实际披露的内含价值替换**，否则 P/EV 绝对值会有偏差（趋势仍可参考）。

---

## 六、数据源

| 指标 | 主源 | 备源 |
|---|---|---|
| 10Y 国债收益率 | 东财数据中心 `RPTA_WEB_TREASURYYIELD`（源：中国债券信息网） | — |
| 行情 / PE / PB / 总市值 | 腾讯 `qt.gtimg.cn` | 东财 `push2` |
| 日 K（前复权） | 腾讯 `fqkline` | 东财 `push2his` |
| 主力资金流 | 东财 `push2his/fflow` | 无（取不到就标「未取到」） |

所有请求带节流（间隔 ≥0.9s）和指数退避重试，避免被限流。
任一项取不到会在报告里明确标注，**不会用旧值冒充**。

---

## 七、文件说明

```
tracker.py                      主脚本（纯标准库）
config.json                     阈值与人工维护项
data/history.json               上期快照，供环比（Actions 每次跑完自动提交）
reports/                        生成的 HTML 报告与 Markdown 摘要
.github/workflows/track.yml     定时配置
```

---

> 本工具基于公开数据分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。
