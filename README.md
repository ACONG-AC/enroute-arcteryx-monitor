# Enroute Arc'teryx Stock Monitor

用 GitHub Actions **无服务器**定时监控 [enroute.run](https://enroute.run) 上 **Arc’teryx（始祖鸟）** 全品类商品的库存变化（从无到有 / 从有到无），并通过 **Discord Webhook** 发送通知。  
脚本会把当前库存快照保存为 `snapshot.json` 并提交到仓库，实现跨运行持久化。

## 功能
- 自动遍历 Arc’teryx 品牌集合页，抓取所有商品链接
- 解析商品页的 **颜色 / 尺码 / 可下单状态**
- 与上次快照对比，找出 **补货** / **售罄** 变化
- 通过 **Discord Webhook** 发送卡片通知（含商品直达链接）
- 由 GitHub Actions 定时执行，不用自备服务器

## 快速开始

1. **Fork 或创建新仓库**，把本仓库文件放进去。
2. 在仓库 `Settings → Secrets and variables → Actions → New repository secret` 新建：
   - `DISCORD_WEBHOOK_URL` = 你的 Discord Webhook 地址
3. 打开 `Actions` 页，点击 **Enable Actions**（首次需要）。
4. 可手动在 `Actions` 页点击 **Run workflow** 立即测试。
5. 默认每 **10 分钟（UTC）** 自动运行一次。

> 提醒：GitHub Actions 的 `schedule` 以 **UTC** 为准。若要对齐 **America/Denver** 的时段，请换算后修改 `cron`。

## 自定义
- **只推送补货**：在 `monitor_enroute_arcteryx.py` 中，`changes = diff_changes(...)` 后追加：
  ```python
  changes = [c for c in changes if c[2] is True]
