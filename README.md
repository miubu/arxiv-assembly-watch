# arXiv 装配规划论文助手

这个 Codex 技能每天看两类新论文：装配工艺与序列规划，以及神经网络方法。它会挑出与你课题相关的几篇，读正文中的方法和实验，写一段中文评述。入选论文的原文、笔记和 PDF2zh 译本放在 Zotero 的「arXiv 每日精选」里。邮件用于提醒最值得精读的论文和需要处理的问题。

## 安装

1. 下载发布页中的 `arxiv-assembly-watch-v1.0.0.zip`，将里面的 `arxiv-assembly-watch` 文件夹解压到 `%USERPROFILE%\.codex\skills\`。解压后应能直接看到 `arxiv-assembly-watch\SKILL.md`。[下载发布包](https://github.com/miubu/arxiv-assembly-watch/releases/latest)
2. 在技能文件夹内，把 `profile.example.json` 复制为 `profile.json`。按自己的课题修改研究方向；需要邮件时填写 `alert_to`。初始精读配置为 `gpt-6.2-sol` 和 `high`，可按自己的模型权限调整这两个字段。
3. 在 Codex 中打开一个存放文献的 Windows 项目文件夹，新建聊天后输入下方的示例。此项目中的 `.arxiv-watch` 保存论文、简报和可续跑的任务状态。

> 使用 $arxiv-assembly-watch 帮我筛一下最近三天的论文。先告诉我哪篇值得读，以及它对我的装配序列规划有什么用。

需要每天运行时，再告诉 Codex「每天北京时间 14:15 在这个项目运行 $arxiv-assembly-watch」。定时任务由使用者在自己的 Codex 中创建。

## 接入 Zotero 和邮件

这套流程已在 Windows、WSL 和 Python 3.12 的组合下验证。Python 脚本使用标准库。在 WSL 安装 Poppler 后，可用 `pdftotext` 读取全文；也可以从相同版本的 arXiv HTML 阅读。

- **Zotero 入库**。使用 Zotero 10 或更新版本，在「设置 → 高级」打开「允许此计算机上的其他应用程序与 Zotero 通信」。从技能文件夹打开 Windows 终端，运行 `python scripts/zotero.py authorize`，在 Zotero 弹窗里选择「始终允许」。这一步完成后，每篇论文会有自己的原文和子笔记。[Zotero 本地 API](https://www.zotero.org/support/dev/web_api/v3/local_api)
- **中文译本**。先在 Zotero 中用 PDF2zh 成功翻译一篇 PDF，并将目标语言设为中文。技能沿用插件配置，将入选论文发给你设置的翻译服务。已验证的插件版本为 4.1.8。[Zotero PDF2zh](https://github.com/guaguastandup/zotero-pdf2zh)
- **邮件**。用同一 QQ 邮箱收发时，在 `alert_to` 填入自己的 QQ 邮箱，在 Windows 终端运行 `python scripts/qq_mail.py`，在本机窗口输入 SMTP 授权码。凭据由 Windows 为当前用户加密保存。其他 SMTP 服务可配置 `SMTP_USER`、`SMTP_PASSWORD`、`SMTP_HOST`，按服务商要求设置 `SMTP_PORT` 和 `SMTP_TLS`（`ssl` 或 `starttls`）。

## 验证安装

在技能文件夹中执行以下命令。两项检查均可离线运行。

```text
python -B scripts/discover.py --self-test
python -B -m unittest discover -s tests
```

检索逻辑改编自 TideDra 的 zotero-arxiv-daily，保留了摘要清洗和交叉公告处理。发布包包含完整源码和 AGPL-3.0 许可证。[上游固定版本](https://github.com/TideDra/zotero-arxiv-daily/tree/35c23ec44617b7595b30ac8e5bf4326c6cfd2336) · [许可证](LICENSE)
