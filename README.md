# Codex Quota Forecast Skill

无需打开 ChatGPT Web，直接在 Codex 中查看官方额度百分比、每日 Credits、Tokens、缓存命中率和本周额度预测。

这是一个非官方的 Codex Skill。它复用当前 Codex 登录状态，通过本地 Codex app-server 获取官方额度窗口，再请求 Codex Web 使用的私有日统计接口，生成适合直接阅读或二次处理的报告。

## 功能

- 官方主额度：已用比例、剩余比例、周期开始和重置时间
- 本周期统计：Credits、总/输入/缓存/非缓存/输出 Tokens、缓存命中率、Turns、Threads
- 每日历史：按天查看 Credits、Tokens、缓存和折算价值
- 周度预测：预测周总 Credits、额度需求、消耗速度和可能耗尽时间
- 独立额度桶：单独展示模型专属额度，避免和主额度混算
- 两种输出：中文文本报告和完整 JSON
- 不打开浏览器，不保存或输出 bearer token

## 安装

要求：

- 已安装并登录 Codex
- `codex` 命令位于 `PATH`
- Python 3.10+
- 能够访问 `chatgpt.com`

克隆仓库并将 Skill 目录复制到个人 Skills 目录：

```bash
git clone https://github.com/pikapikaspeedup/codex-quota-forecast-skill.git
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R codex-quota-forecast-skill/codex-quota-forecast \
  "${CODEX_HOME:-$HOME/.codex}/skills/"
```

重新打开 Codex 后，可以直接说：

```text
使用 $codex-quota-forecast 查看我当前的 Codex 额度、每日用量和本周预测。
```

## 直接运行

文本报告：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-quota-forecast/scripts/quota_forecast.py"
```

完整 JSON：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-quota-forecast/scripts/quota_forecast.py" --json
```

常用参数：

```text
--lookback-days N       历史回看天数，默认 45
--history-limit N       文本模式展开的历史行数，默认 14；0 表示全部
--usd-per-credit VALUE  Credit 折算美元单价，默认 0.04
--timeout SECONDS       app-server 和 HTTP 超时
--proxy URL             显式指定 HTTPS 代理
--no-proxy              禁用代理自动检测
--codex-bin PATH        显式指定 Codex 可执行文件
```

## 数据与预测口径

- `used_percent`、`remaining_percent` 和重置时间来自 Codex 本地 app-server 返回的官方额度状态。
- Credits 和 Token 明细来自 `daily-workspace-usage-counts` 私有日统计接口。
- 周总 Credits 预测采用“本周期已统计 Credits ÷ 官方已用比例”。
- 耗尽时间采用当前周期内官方额度消耗速度作线性外推。
- 默认价值折算为 `$40 / 1000 Credits`，仅用于估算，不代表账单或公开定价。
- 日统计可能延迟；报告会同时显示抓取时间、最新统计日期和延迟天数。
- 日统计按自然日聚合，而官方额度可能在一天中的某个时间重置，所以周期首日存在边界误差。

## 隐私与安全

脚本会从当前 Codex 的 `auth.json` 读取 OAuth access token 和 account ID，仅在进程内存中用于请求：

- token 不会打印到终端
- token 不会保存到本仓库或新的文件
- 使用 `curl` 时，鉴权配置通过标准输入传递，不放入命令参数
- 报告中的代理地址会删除用户名、密码、路径和查询参数

请只在你信任的本机环境中运行。不要把 `auth.json`、脚本调试日志或包含账号数据的 JSON 报告提交到公开仓库。

## 限制

- 依赖未公开的 ChatGPT/Codex 私有接口，接口或字段发生变化时可能失效。
- 历史模型明细可能提供 Turns，但模型级 Credits 目前可能为零；不要据此分摊模型成本。
- 预测是线性估算，不是服务端承诺。
- 本项目不是 OpenAI 官方项目，也不与 OpenAI 存在隶属关系。

## 参考与致谢

本 Skill 的日统计接口、Credits/Token 字段理解、缓存命中率和价值展示思路参考了 [Wangnov/codex-meter](https://github.com/Wangnov/codex-meter) 的实现，尤其是其 [README](https://github.com/Wangnov/codex-meter/blob/main/README.md) 所描述的 Codex Meter 数据口径。

`codex-meter` 是一个在 ChatGPT Codex analytics 页面中提供可视化的 Chrome 扩展；本仓库则把相关统计能力做成无需打开 Web 的 Codex Skill，并增加本地 app-server 额度读取、命令行报告和周度预测。上游项目采用 MIT License，相关版权与许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## License

[MIT](LICENSE)

---

## English

An unofficial Codex Skill that reports official quota percentage, daily Credits, token/cache usage, reset time, and a weekly forecast without opening ChatGPT Web.

Install the `codex-quota-forecast` directory under `$CODEX_HOME/skills` or `~/.codex/skills`, then invoke `$codex-quota-forecast` in Codex. The script uses the existing local Codex login, keeps credentials in memory, and emits either a readable report or JSON.

This project depends on private ChatGPT/Codex endpoints and may require maintenance when those endpoints change. Forecasts and the default `$40/1000 Credits` conversion are estimates, not billing guarantees.

